"""Tests for `j-cli setup git`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "git"] + args, catch_exceptions=False)


def _hooks_path_config(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


# ---------------------------------------------------------------------------
# --local scope
# ---------------------------------------------------------------------------

class TestLocalScope:
    def test_local_creates_hook_in_git_hooks(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = _invoke(runner, ["--local"])

        assert result.exit_code == 0
        hook = git_repo / ".git" / "hooks" / "pre-commit"
        assert hook.exists()
        assert _is_executable(hook)
        content = hook.read_text()
        assert "#!/usr/bin/env bash" in content
        assert "j-cli _hooks pre-commit-pair-sync" in content

    def test_local_does_not_set_core_hookspath(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        assert _hooks_path_config(git_repo) is None

    def test_local_overwrite_notice(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        hook = git_repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho old\n", encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local"])

        assert result.exit_code == 0
        combined = (result.output or "") + (result.stderr or "")
        assert "overwrote" in combined.lower()
        # Content should be replaced
        assert "j-cli _hooks pre-commit-pair-sync" in hook.read_text()


# ---------------------------------------------------------------------------
# --project scope (default)
# ---------------------------------------------------------------------------

class TestProjectScope:
    def test_project_creates_hook_in_scripts(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = _invoke(runner, ["--project"])

        assert result.exit_code == 0
        hook = git_repo / ".githooks" / "pre-commit"
        assert hook.exists()
        assert _is_executable(hook)
        content = hook.read_text()
        assert "j-cli _hooks pre-commit-pair-sync" in content

    def test_project_sets_core_hookspath(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--project"])
        assert _hooks_path_config(git_repo) == ".githooks"

    def test_project_is_default_scope(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = _invoke(runner, [])  # no --local or --project

        assert result.exit_code == 0
        hook = git_repo / ".githooks" / "pre-commit"
        assert hook.exists()
        assert _hooks_path_config(git_repo) == ".githooks"

    def test_project_overrides_existing_hookspath_with_notice(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        subprocess.run(
            ["git", "config", "--local", "core.hooksPath", "old-hooks"],
            cwd=str(git_repo), check=True,
        )

        runner = CliRunner()
        result = _invoke(runner, ["--project"])

        assert result.exit_code == 0
        assert _hooks_path_config(git_repo) == ".githooks"
        combined = (result.output or "") + (result.stderr or "")
        assert "old-hooks" in combined


# ---------------------------------------------------------------------------
# Not a git repo -> exit 1
# ---------------------------------------------------------------------------

class TestNotGitRepo:
    def test_not_git_repo_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 1
        assert "NOT_A_GIT_REPO" in result.output


# ---------------------------------------------------------------------------
# .gitignore managed block
# ---------------------------------------------------------------------------

class TestGitignoreBlock:
    def test_creates_gitignore_with_block(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local"])

        gi = (git_repo / ".gitignore").read_text()
        assert "*.ipynb" in gi
        assert "# >>> jcli managed (git hooks) >>>" in gi
        assert "# <<< jcli managed (git hooks) <<<" in gi

    def test_appends_to_existing_gitignore(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        gi_path = git_repo / ".gitignore"
        gi_path.write_text("__pycache__/\n*.pyc\n", encoding="utf-8")

        runner = CliRunner()
        _invoke(runner, ["--local"])

        content = gi_path.read_text()
        assert "__pycache__/" in content
        assert "*.pyc" in content
        assert "*.ipynb" in content

    def test_replaces_existing_managed_block(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        gi_path = git_repo / ".gitignore"
        gi_path.write_text(
            "build/\n"
            "# >>> jcli managed (git hooks) >>>\n"
            "*.csv\n"
            "# <<< jcli managed (git hooks) <<<\n"
            "dist/\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        _invoke(runner, ["--local"])

        content = gi_path.read_text()
        assert "build/" in content
        assert "dist/" in content
        assert "*.ipynb" in content
        assert "*.csv" not in content

    def test_exactly_one_trailing_newline(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        raw = (git_repo / ".gitignore").read_bytes()
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_local_idempotent(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        first_hook = (git_repo / ".git" / "hooks" / "pre-commit").read_bytes()
        first_gi = (git_repo / ".gitignore").read_bytes()

        _invoke(runner, ["--local"])
        assert (git_repo / ".git" / "hooks" / "pre-commit").read_bytes() == first_hook
        assert (git_repo / ".gitignore").read_bytes() == first_gi

    def test_project_idempotent(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--project"])
        hook_path = git_repo / ".githooks" / "pre-commit"
        first_hook = hook_path.read_bytes()
        first_gi = (git_repo / ".gitignore").read_bytes()

        _invoke(runner, ["--project"])
        assert hook_path.read_bytes() == first_hook
        assert (git_repo / ".gitignore").read_bytes() == first_gi


# ---------------------------------------------------------------------------
# --include globs written into shim
# ---------------------------------------------------------------------------

class TestIncludeGlobs:
    def test_single_include_in_shim(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local", "--include", "src/*.py"])

        hook = git_repo / ".git" / "hooks" / "pre-commit"
        content = hook.read_text()
        assert "--include" in content
        assert "src/*.py" in content

    def test_multiple_includes_in_shim(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local", "--include", "src/*.py", "--include", "tests/*.py"])

        hook = git_repo / ".git" / "hooks" / "pre-commit"
        content = hook.read_text()
        assert content.count("--include") == 2
        assert "src/*.py" in content
        assert "tests/*.py" in content

    def test_glob_with_spaces_is_quoted(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local", "--include", "my dir/*.py"])

        hook = git_repo / ".git" / "hooks" / "pre-commit"
        content = hook.read_text()
        # shlex.quote wraps in single quotes when spaces present
        assert "'my dir/*.py'" in content

    def test_replacing_include_args_is_idempotent(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        _invoke(runner, ["--local", "--include", "old/*.py"])
        _invoke(runner, ["--local", "--include", "new/*.py"])

        hook = git_repo / ".git" / "hooks" / "pre-commit"
        content = hook.read_text()
        assert "new/*.py" in content
        assert "old/*.py" not in content


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_json_ok_structure(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "git", "--local"], catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "hook_path" in data
        assert "gitignore_path" in data
        assert data["scope"] == "local"
        assert isinstance(data["include"], list)

    def test_json_project_scope(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "git", "--project"], catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["scope"] == "project"

    def test_json_include_list(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "setup", "git", "--local", "--include", "src/*.py"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["include"] == ["src/*.py"]
