"""Tests for `j-cli _hooks pre-commit-pair-sync`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo in tmp_path."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=str(repo), check=True, capture_output=True)


def _invoke(runner: CliRunner, args: list[str] | None = None):
    return runner.invoke(
        main,
        ["_hooks", "pre-commit-pair-sync"] + (args or []),
        catch_exceptions=False,
    )


def _combined(result) -> str:
    """Merge stdout + stderr from a CliRunner result."""
    return (result.output or "") + (result.stderr or "")


def _make_py(path: Path, *sources: str) -> None:
    lines = [
        "# ---\n", "# jupyter:\n", "#   kernelspec:\n",
        "#     name: python3\n", "# ---\n\n",
    ]
    for src in sources:
        lines.append(f"# %%\n{src}\n\n")
    path.write_text("".join(lines), encoding="utf-8")


def _make_ipynb(path: Path, *sources: str) -> None:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "name": "python3", "display_name": "Python 3", "language": "python",
    }
    for src in sources:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    path.write_text(nbformat.writes(nb), encoding="utf-8")


def _staged_files(repo: Path) -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    return [p for p in r.stdout.splitlines() if p.strip()]


# ---------------------------------------------------------------------------
# Staged .ipynb -> blocked
# ---------------------------------------------------------------------------

class TestStagedIpynbBlocked:
    def test_staged_ipynb_exits_1(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_ipynb(git_repo / "foo.ipynb", "x = 1")
        _git(git_repo, "git", "add", "foo.ipynb")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 1
        assert "foo.ipynb" in (result.stderr or "")

    def test_staged_ipynb_lists_multiple(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        for name in ("a.ipynb", "b.ipynb"):
            _make_ipynb(git_repo / name, "x = 1")
            _git(git_repo, "git", "add", name)

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 1
        stderr = result.stderr or ""
        assert "a.ipynb" in stderr
        assert "b.ipynb" in stderr


# ---------------------------------------------------------------------------
# Staged .py with no paired .ipynb -> no-op, exit 0
# ---------------------------------------------------------------------------

class TestNoPair:
    def test_py_without_ipynb_skipped(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "solo.py", "x = 1")
        _git(git_repo, "git", "add", "solo.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# in_sync -> exit 0, silent
# ---------------------------------------------------------------------------

class TestInSync:
    def test_in_sync_pair_silent(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1")
        _git(git_repo, "git", "add", "nb.py")

        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            runner = CliRunner()
            result = _invoke(runner)

        assert result.exit_code == 0
        assert (result.stderr or "").strip() == ""


# ---------------------------------------------------------------------------
# Initial sync: .py missing on disk, .ipynb exists -> create .py + git add
# ---------------------------------------------------------------------------

class TestInitialSync:
    def test_initial_sync_creates_py(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)

        # Stage foo.py temporarily, then delete it from disk
        _make_py(git_repo / "foo.py", "x = 1")
        _git(git_repo, "git", "add", "foo.py")
        (git_repo / "foo.py").unlink()

        # Create the ipynb counterpart
        _make_ipynb(git_repo / "foo.ipynb", "x = 42")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 0
        assert (git_repo / "foo.py").exists()
        # foo.py must now be re-staged
        assert "foo.py" in _staged_files(git_repo)
        assert "initial sync" in (result.stderr or "")


# ---------------------------------------------------------------------------
# merged: py_needs_update -> write .py + git add, exit 0
# ---------------------------------------------------------------------------

class TestMergedPyNeedsUpdate:
    def test_py_updated_and_staged(self, git_repo, monkeypatch):
        """Two cells: py changes cell1, ipynb changes cell0 (non-conflicting).
        Merge needs py to pick up cell0 from ipynb → py_needs_update=True → git add."""
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1", "y = 2")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1", "y = 2")
        _git(git_repo, "git", "add", "nb.py", "nb.ipynb")
        _git(git_repo, "git", "commit", "-m", "init")

        # py: change cell1 (y=2→y=20), stage it
        # ipynb: change cell0 (x=1→x=10), don't stage
        _make_py(git_repo / "nb.py", "x = 1", "y = 20")
        _make_ipynb(git_repo / "nb.ipynb", "x = 10", "y = 2")
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 0
        # py must have x=10 (from ipynb) AND y=20 (own change)
        from jupyter_jcli.parser import parse_py_percent
        cells = parse_py_percent(str(git_repo / "nb.py")).cells
        assert cells[0].source == "x = 10"
        assert cells[1].source == "y = 20"
        # py must be re-staged
        assert "nb.py" in _staged_files(git_repo)
        assert "auto-synced" in (result.stderr or "")


# ---------------------------------------------------------------------------
# merged: ipynb_needs_update -> write .ipynb (not staged), exit 0
# ---------------------------------------------------------------------------

class TestMergedIpynbNeedsUpdate:
    def test_ipynb_updated_not_staged(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1")
        _git(git_repo, "git", "add", "nb.py", "nb.ipynb")
        _git(git_repo, "git", "commit", "-m", "init")

        # Change py (x=99) and stage it; ipynb still has x=1
        _make_py(git_repo / "nb.py", "x = 99")
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 0
        # .ipynb should now have x = 99 on disk
        nb = nbformat.read(str(git_repo / "nb.ipynb"), as_version=4)
        non_empty = [c for c in nb.cells if c.source.strip()]
        assert non_empty[0].source == "x = 99"
        # .ipynb must NOT be staged
        assert "nb.ipynb" not in _staged_files(git_repo)
        assert "auto-synced" in (result.stderr or "")


# ---------------------------------------------------------------------------
# conflict (both sides changed same cell) -> exit 1
# ---------------------------------------------------------------------------

class TestConflict:
    def test_conflict_exits_1_with_cell_index(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1")
        _git(git_repo, "git", "add", "nb.py", "nb.ipynb")
        _git(git_repo, "git", "commit", "-m", "init")

        # Both sides change cell 0 to different values
        _make_py(git_repo / "nb.py", "x = 10")
        _make_ipynb(git_repo / "nb.ipynb", "x = 99")
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 1
        stderr = result.stderr or ""
        assert "conflict" in stderr.lower() or "conflict" in (result.output or "").lower()
        # Mention j-cli convert
        combined = _combined(result)
        assert "j-cli convert" in combined


# ---------------------------------------------------------------------------
# drift_only (no git base) -> exit 1
# ---------------------------------------------------------------------------

class TestDriftOnly:
    def test_drift_only_exits_1(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        # No commit → no git base
        _make_py(git_repo / "nb.py", "x = 1")
        _make_ipynb(git_repo / "nb.ipynb", "x = 99")  # different
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 1
        combined = _combined(result)
        assert "git base" in combined or "pick a side" in combined.lower()
        assert "j-cli convert" in combined


# ---------------------------------------------------------------------------
# Two sides change different cells -> merged, both written, exit 0
# ---------------------------------------------------------------------------

class TestMergeDifferentCells:
    def test_different_cells_merged(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1", "y = 2")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1", "y = 2")
        _git(git_repo, "git", "add", "nb.py", "nb.ipynb")
        _git(git_repo, "git", "commit", "-m", "init")

        # py: cell 0 → x=10; ipynb: cell 1 → y=20
        _make_py(git_repo / "nb.py", "x = 10", "y = 2")
        _make_ipynb(git_repo / "nb.ipynb", "x = 1", "y = 20")
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 0

        from jupyter_jcli.parser import parse_py_percent
        py_cells = parse_py_percent(str(git_repo / "nb.py")).cells
        assert py_cells[0].source == "x = 10"
        assert py_cells[1].source == "y = 20"

        nb = nbformat.read(str(git_repo / "nb.ipynb"), as_version=4)
        non_empty = [c for c in nb.cells if c.source.strip()]
        assert non_empty[0].source == "x = 10"
        assert non_empty[1].source == "y = 20"


# ---------------------------------------------------------------------------
# --include filter
# ---------------------------------------------------------------------------

class TestIncludeFilter:
    def test_include_matching_processes_file(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        sub = git_repo / "nb"
        sub.mkdir()
        _make_py(sub / "script.py", "x = 1")
        _make_ipynb(sub / "script.ipynb", "x = 99")  # drift → exit 1
        _git(git_repo, "git", "add", "nb/script.py")

        runner = CliRunner()
        result = _invoke(runner, ["--include", "nb/*.py"])

        # drift_only → exit 1 (confirming the file was processed)
        assert result.exit_code == 1

    def test_include_not_matching_skips_file(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        sub = git_repo / "nb"
        sub.mkdir()
        _make_py(sub / "script.py", "x = 1")
        _make_ipynb(sub / "script.ipynb", "x = 99")  # would drift
        _git(git_repo, "git", "add", "nb/script.py")

        runner = CliRunner()
        # Pattern that does NOT match nb/script.py
        result = _invoke(runner, ["--include", "other/*.py"])

        # File was skipped → no error
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Fail-open: non-git dir / git not in PATH
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_non_git_dir_exits_0(self, tmp_path, monkeypatch):
        """Directory without git init → fail-open."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner)
        assert result.exit_code == 0
        combined = _combined(result)
        assert "git" in combined.lower() or "repo" in combined.lower()

    def test_git_not_found_exits_0(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            runner = CliRunner()
            result = _invoke(runner)
        assert result.exit_code == 0
        combined = _combined(result)
        assert "git" in combined.lower()


# ---------------------------------------------------------------------------
# Fail-closed: non-UTF-8 .ipynb -> exit 1
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_non_utf8_ipynb_exits_1(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        _make_py(git_repo / "nb.py", "x = 1")
        # Write bytes that are invalid UTF-8
        (git_repo / "nb.ipynb").write_bytes(b"{\xff\xfe\x00invalid-utf8}")
        _git(git_repo, "git", "add", "nb.py")

        runner = CliRunner()
        result = _invoke(runner)

        assert result.exit_code == 1
