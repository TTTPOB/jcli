"""Tests for jupyter_jcli.pair_baseline."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jupyter_jcli import pair_baseline
from tests.test_drift import _make_py_text


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


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _git_env(ts: int) -> dict[str, str]:
    env = os.environ.copy()
    stamp = f"@{ts} +0000"
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    return env


def _write_and_commit(repo: Path, rel_path: str, text: str, ts: int, *, add: bool = True) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if add:
        _git(repo, "add", rel_path)
    _git(repo, "commit", "-m", f"update {rel_path}", env=_git_env(ts))
    return path


def _ref_name(rel_path: str) -> str:
    return pair_baseline._ref_name(Path(rel_path).as_posix())


class TestReadWriteBaseline:
    def test_empty_repo_without_head_returns_none(self, git_repo: Path) -> None:
        py_path = git_repo / "nb.py"
        assert pair_baseline.read_baseline(py_path) is None

    def test_head_without_ref_returns_head_text(self, git_repo: Path) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 1")

    def test_ref_without_head_file_returns_ref_text(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _git(git_repo, "commit", "--allow-empty", "-m", "init", env=_git_env(100))
        py_path = git_repo / "ghost.py"
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 2")) is True
        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 2")

    def test_newer_ref_wins_and_subject_is_recorded(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 10")) is True

        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 10")
        ref_name = _ref_name("nb.py")
        show = _git(git_repo, "show", f"{ref_name}:file")
        subject = _git(git_repo, "log", "-1", "--format=%s", ref_name)
        assert show.stdout == _make_py_text("x = 10")
        assert subject.stdout.strip() == "jcli pair-sync baseline: nb.py"

    def test_paths_with_spaces_and_unicode_round_trip(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rel_path = "子 目录/测试 notebook.py"
        py_path = _write_and_commit(git_repo, rel_path, _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 3")) is True
        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 3")

    def test_write_baseline_works_without_git_identity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        _git(tmp_path, "commit", "--allow-empty", "-m", "init", env=_git_env(100))
        py_path = tmp_path / "nb.py"
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 4")) is True
        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 4")

    def test_non_git_directory_fails_open(self, tmp_path: Path) -> None:
        py_path = tmp_path / "nb.py"
        assert pair_baseline.read_baseline(py_path) is None
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 1")) is False


class TestLazyEviction:
    def test_newer_head_evicts_ref_and_returns_head(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 10")) is True

        _write_and_commit(git_repo, "nb.py", _make_py_text("x = 20"), 200)

        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 20")
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert refs.stdout.strip() == ""

    def test_delete_failure_does_not_block_head_fallback(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 10")) is True

        _write_and_commit(git_repo, "nb.py", _make_py_text("x = 20"), 200)

        def _boom(_repo_root: Path, _ref_name: str) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(pair_baseline, "_delete_ref", _boom)
        assert pair_baseline.read_baseline(py_path) == _make_py_text("x = 20")


class TestGc:
    def test_gc_dry_run_reports_without_deleting(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 10")) is True
        _write_and_commit(git_repo, "nb.py", _make_py_text("x = 20"), 200)

        removed, kept = pair_baseline.gc_stale_refs(git_repo, dry_run=True)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert removed == 1
        assert kept == 0
        assert refs.stdout.strip() != ""

    def test_gc_removes_orphan_ref_for_deleted_path(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _git(git_repo, "commit", "--allow-empty", "-m", "init", env=_git_env(100))
        ghost_path = git_repo / "ghost.py"
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(ghost_path, _make_py_text("x = 3")) is True

        removed, kept = pair_baseline.gc_stale_refs(git_repo, dry_run=False)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert removed == 1
        assert kept == 0
        assert refs.stdout.strip() == ""

    def test_gc_keeps_ref_newer_than_head(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        py_path = _write_and_commit(git_repo, "nb.py", _make_py_text("x = 1"), 100)
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py_path, _make_py_text("x = 10")) is True

        removed, kept = pair_baseline.gc_stale_refs(git_repo, dry_run=False)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert removed == 0
        assert kept == 1
        assert refs.stdout.strip() != ""

    def test_gc_removes_invalid_subject_refs(self, git_repo: Path) -> None:
        _git(git_repo, "commit", "--allow-empty", "-m", "init", env=_git_env(100))
        blob_sha = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(git_repo),
            input=b"plain\n",
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").strip()
        invalid_tree = subprocess.run(
            ["git", "mktree"],
            cwd=str(git_repo),
            input=f"100644 blob {blob_sha}\tfile\n",
            check=True,
            capture_output=True,
            text=True,
            env=_git_env(150),
        ).stdout.strip()
        commit_sha = _git(
            git_repo,
            "commit-tree",
            invalid_tree,
            "-m",
            "not a jcli ref",
            env=_git_env(150),
        ).stdout.strip()
        _git(git_repo, "update-ref", "refs/jcli/pair-sync/badsubject", commit_sha)

        removed, kept = pair_baseline.gc_stale_refs(git_repo, dry_run=False)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert removed == 1
        assert kept == 0
        assert refs.stdout.strip() == ""
