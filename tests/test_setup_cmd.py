"""Tests for `j-cli setup claude`."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "claude"] + args, catch_exceptions=False)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _has_hook(settings: dict) -> bool:
    """Return True if the nbconvert-guard hook block is present in settings."""
    for block in settings.get("hooks", {}).get("PreToolUse", []):
        for entry in block.get("hooks", []):
            if entry.get("_jcli_managed") == "nbconvert-guard":
                return True
    return False


# ---------------------------------------------------------------------------
# Scope routing
# ---------------------------------------------------------------------------

class TestScopeRouting:
    def test_local_is_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 0
        target = tmp_path / ".claude" / "settings.local.json"
        assert target.exists()
        assert _has_hook(_read_json(target))

    def test_project_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--project"])
        assert result.exit_code == 0
        target = tmp_path / ".claude" / "settings.json"
        assert target.exists()
        assert _has_hook(_read_json(target))

    def test_user_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        runner = CliRunner()
        result = _invoke(runner, ["--user"])
        assert result.exit_code == 0
        target = tmp_path / ".claude" / "settings.json"
        assert target.exists()
        assert _has_hook(_read_json(target))

    def test_local_explicit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        target = tmp_path / ".claude" / "settings.local.json"
        assert target.exists()

    def test_auto_creates_parent_dir(self, tmp_path, monkeypatch):
        """Parent .claude/ directory is created automatically."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        assert not claude_dir.exists()
        runner = CliRunner()
        _invoke(runner, ["--local"])
        assert claude_dir.is_dir()


# ---------------------------------------------------------------------------
# Merge / de-dupe
# ---------------------------------------------------------------------------

class TestMerge:
    def test_idempotent_no_duplicate(self, tmp_path, monkeypatch):
        """Running setup claude twice must not duplicate the hook entry."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".claude" / "settings.local.json")
        blocks = settings.get("hooks", {}).get("PreToolUse", [])
        managed_count = sum(
            1
            for block in blocks
            for entry in block.get("hooks", [])
            if entry.get("_jcli_managed") == "nbconvert-guard"
        )
        assert managed_count == 1

    def test_preserves_existing_keys(self, tmp_path, monkeypatch):
        """Unrelated settings keys are not clobbered."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "permissions": {"allow": ["Read"]},
            "env": {"MY_VAR": "hello"},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{"type": "command", "command": "echo pre-read"}],
                    }
                ]
            },
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = _read_json(target)

        assert result["permissions"] == {"allow": ["Read"]}
        assert result["env"] == {"MY_VAR": "hello"}
        # Original Read hook still present
        pre = result["hooks"]["PreToolUse"]
        assert any(b.get("matcher") == "Read" for b in pre)
        # j-cli hook also present
        assert _has_hook(result)

    def test_updates_managed_entry_in_place(self, tmp_path, monkeypatch):
        """Re-running updates the managed entry without appending a new block."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "old-command", "_jcli_managed": "nbconvert-guard"},
                            {"type": "command", "command": "other-hook"},
                        ],
                    }
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = _read_json(target)

        blocks = result["hooks"]["PreToolUse"]
        assert len(blocks) == 1  # no new block appended
        inner = blocks[0]["hooks"]
        # Managed entry updated; other-hook still present
        managed = [e for e in inner if e.get("_jcli_managed") == "nbconvert-guard"]
        assert len(managed) == 1
        assert managed[0]["command"] == "j-cli _hooks nbconvert-guard"
        other = [e for e in inner if e.get("command") == "other-hook"]
        assert len(other) == 1

    def test_corrupt_json_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        target = claude_dir / "settings.local.json"
        target.write_text("{not valid json", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(main, ["setup", "claude", "--local"])
        assert result.exit_code == 1
        assert "SETTINGS_INVALID" in result.output or "SETTINGS_INVALID" in (result.stderr or "")

    def test_empty_existing_file(self, tmp_path, monkeypatch):
        """An empty file is treated the same as a missing file."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        target = claude_dir / "settings.local.json"
        target.write_text("", encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert _has_hook(_read_json(target))


# ---------------------------------------------------------------------------
# JSON output mode
# ---------------------------------------------------------------------------

class TestJsonMode:
    def test_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "claude", "--local"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "path" in data
