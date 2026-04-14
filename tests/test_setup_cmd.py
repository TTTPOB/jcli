"""Tests for `j-cli setup claude`."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.setup_cmd import Scope


# ---------------------------------------------------------------------------
# Scope enum behaviour
# ---------------------------------------------------------------------------

class TestScopeEnum:
    def test_members_exist(self):
        assert Scope.USER == "user"
        assert Scope.PROJECT == "project"
        assert Scope.LOCAL == "local"

    def test_str_inheritance(self):
        assert isinstance(Scope.USER, str)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            Scope("bogus")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "claude"] + args, catch_exceptions=False)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _has_hook(settings: dict) -> bool:
    """Return True if the notebook-exec-guard hook block is present in settings."""
    for block in settings.get("hooks", {}).get("PreToolUse", []):
        for entry in block.get("hooks", []):
            if entry.get("_jcli_managed") == "notebook-exec-guard":
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
            if entry.get("_jcli_managed") == "notebook-exec-guard"
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
        """Re-running updates the managed entry in the Bash block without duplicating it."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "old-command", "_jcli_managed": "notebook-exec-guard"},
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

        all_entries = [
            e
            for block in result["hooks"]["PreToolUse"]
            if block.get("matcher") == "Bash"
            for e in block.get("hooks", [])
        ]
        # notebook-exec-guard updated, not duplicated
        managed = [e for e in all_entries if e.get("_jcli_managed") == "notebook-exec-guard"]
        assert len(managed) == 1
        assert managed[0]["command"] == "j-cli _hooks notebook-exec-guard"
        # other-hook preserved
        assert any(e.get("command") == "other-hook" for e in all_entries)
        # python-run-guard also installed (may be a separate Bash block)
        assert _count_managed(result, "python-run-guard") == 1
        # pair-drift-guard blocks also installed
        assert _has_hook(result)

    def test_upgrade_replaces_legacy_managed_entry(self, tmp_path, monkeypatch):
        """Setup with a new version replaces a legacy-named managed entry in-place."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # Simulate settings written by an older j-cli that used "nbconvert-guard".
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks nbconvert-guard",
                             "_jcli_managed": "nbconvert-guard"},
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

        all_entries = [
            e
            for block in result["hooks"]["PreToolUse"]
            if block.get("matcher") == "Bash"
            for e in block.get("hooks", [])
        ]
        # Legacy entry gone, replaced by current name (exactly once).
        assert not any(e.get("_jcli_managed") == "nbconvert-guard" for e in all_entries)
        assert _has_hook(result)
        managed = [e for e in all_entries if e.get("_jcli_managed") == "notebook-exec-guard"]
        assert len(managed) == 1
        assert managed[0]["command"] == "j-cli _hooks notebook-exec-guard"
        # Unrelated hook preserved.
        assert any(e.get("command") == "other-hook" for e in all_entries)
        # python-run-guard also installed.
        assert _count_managed(result, "python-run-guard") == 1

    def test_upgrade_deduplicates_both_old_and_new(self, tmp_path, monkeypatch):
        """If both legacy and current entries exist (the scenario the user hit), only one survives."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "j-cli _hooks nbconvert-guard",
                                   "_jcli_managed": "nbconvert-guard"}],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "j-cli _hooks notebook-exec-guard",
                                   "_jcli_managed": "notebook-exec-guard"}],
                    },
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = _read_json(target)

        all_managed = [
            e
            for block in result["hooks"]["PreToolUse"]
            for e in block.get("hooks", [])
            if e.get("_jcli_managed") in ("nbconvert-guard", "notebook-exec-guard")
        ]
        assert len(all_managed) == 1
        assert all_managed[0]["_jcli_managed"] == "notebook-exec-guard"

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


# ---------------------------------------------------------------------------
# Three-block installation (new blocks for pair-drift-guard)
# ---------------------------------------------------------------------------

def _count_managed(settings: dict, val: str) -> int:
    return sum(
        1
        for block in settings.get("hooks", {}).get("PreToolUse", [])
        for entry in block.get("hooks", [])
        if entry.get("_jcli_managed") == val
    )


def _has_matcher(settings: dict, matcher: str) -> bool:
    return any(
        b.get("matcher") == matcher
        for b in settings.get("hooks", {}).get("PreToolUse", [])
    )


class TestThreeBlocks:
    def test_all_three_blocks_installed(self, tmp_path, monkeypatch):
        """Fresh install creates all four managed hook blocks."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0

        settings = _read_json(tmp_path / ".claude" / "settings.local.json")

        assert _has_hook(settings)  # notebook-exec-guard on Bash
        assert _has_matcher(settings, "Edit|Write")
        assert _has_matcher(settings, "NotebookEdit")
        assert _count_managed(settings, "pair-drift-guard") == 1
        assert _count_managed(settings, "pair-drift-guard-notebook") == 1
        assert _count_managed(settings, "python-run-guard") == 1

    def test_idempotent_three_blocks(self, tmp_path, monkeypatch):
        """Running setup twice does not duplicate any block."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        _invoke(runner, ["--local"])

        settings = _read_json(tmp_path / ".claude" / "settings.local.json")

        assert _count_managed(settings, "notebook-exec-guard") == 1
        assert _count_managed(settings, "pair-drift-guard") == 1
        assert _count_managed(settings, "pair-drift-guard-notebook") == 1
        assert _count_managed(settings, "python-run-guard") == 1

    def test_pair_drift_guard_commands(self, tmp_path, monkeypatch):
        """pair-drift-guard entries point to the correct command."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])

        settings = _read_json(tmp_path / ".claude" / "settings.local.json")
        for block in settings["hooks"]["PreToolUse"]:
            for entry in block.get("hooks", []):
                if entry.get("_jcli_managed") in ("pair-drift-guard", "pair-drift-guard-notebook"):
                    assert entry["command"] == "j-cli _hooks pair-drift-guard"

    def test_legacy_nbconvert_guard_upgraded(self, tmp_path, monkeypatch):
        """Legacy nbconvert-guard entry is replaced even with new pair-drift-guard blocks."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks nbconvert-guard",
                             "_jcli_managed": "nbconvert-guard"},
                        ],
                    }
                ]
            }
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        _invoke(runner, ["--local"])
        settings = _read_json(claude_dir / "settings.local.json")

        assert _count_managed(settings, "nbconvert-guard") == 0
        assert _count_managed(settings, "notebook-exec-guard") == 1
        assert _count_managed(settings, "pair-drift-guard") == 1
        assert _count_managed(settings, "pair-drift-guard-notebook") == 1


# ---------------------------------------------------------------------------
# --remove flag
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_after_install_deletes_file(self, tmp_path, monkeypatch):
        """install then remove leaves no file when the settings only had managed hooks."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        target = tmp_path / ".claude" / "settings.local.json"
        assert target.exists()

        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0
        assert not target.exists()

    def test_remove_preserves_unrelated_hooks(self, tmp_path, monkeypatch):
        """Unrelated user hooks survive; only managed entries are removed."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [{"type": "command", "command": "echo read"}]},
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks notebook-exec-guard",
                             "_jcli_managed": "notebook-exec-guard"},
                            {"type": "command", "command": "user-hook"},
                        ],
                    },
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0
        assert target.exists()

        settings = _read_json(target)
        pre = settings["hooks"]["PreToolUse"]
        assert any(b.get("matcher") == "Read" for b in pre)
        bash_entries = [
            e for b in pre if b.get("matcher") == "Bash"
            for e in b.get("hooks", [])
        ]
        assert any(e.get("command") == "user-hook" for e in bash_entries)
        assert not any(e.get("_jcli_managed") == "notebook-exec-guard" for e in bash_entries)

    def test_remove_preserves_other_top_level_keys(self, tmp_path, monkeypatch):
        """Non-hook top-level keys (permissions, env) survive the remove operation."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "permissions": {"allow": ["Read"]},
            "env": {"MY_VAR": "hello"},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks notebook-exec-guard",
                             "_jcli_managed": "notebook-exec-guard"},
                        ],
                    }
                ]
            },
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0

        settings = _read_json(target)
        assert settings["permissions"] == {"allow": ["Read"]}
        assert settings["env"] == {"MY_VAR": "hello"}
        assert "hooks" not in settings

    def test_remove_kills_legacy_nbconvert_guard(self, tmp_path, monkeypatch):
        """Legacy _jcli_managed: 'nbconvert-guard' entry is also removed."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks nbconvert-guard",
                             "_jcli_managed": "nbconvert-guard"},
                        ],
                    }
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0
        assert not target.exists()

    def test_remove_when_file_missing_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "claude", "--local", "--remove"], catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "noop"

    def test_remove_when_no_managed_entries_is_noop(self, tmp_path, monkeypatch):
        """File with only user hooks is left untouched; status is noop."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [{"type": "command", "command": "echo read"}]},
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "claude", "--local", "--remove"], catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "noop"
        assert data["removed"] == 0
        assert target.exists()

    def test_remove_respects_scope_user(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        runner = CliRunner()
        _invoke(runner, ["--user"])
        target = tmp_path / ".claude" / "settings.json"
        assert target.exists()

        result = _invoke(runner, ["--user", "--remove"])
        assert result.exit_code == 0
        assert not target.exists()

    def test_remove_respects_scope_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--project"])
        project_target = tmp_path / ".claude" / "settings.json"
        assert project_target.exists()

        # A settings.local.json that must not be touched
        local_target = tmp_path / ".claude" / "settings.local.json"
        local_target.write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")

        result = _invoke(runner, ["--project", "--remove"])
        assert result.exit_code == 0
        assert not project_target.exists()
        assert local_target.exists()

    def test_remove_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = runner.invoke(
            main, ["--json", "setup", "claude", "--local", "--remove"], catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["removed"] > 0
        assert "path" in data

    def test_remove_empty_block_pruned(self, tmp_path, monkeypatch):
        """After removal, no empty {matcher: X, hooks: []} blocks remain."""
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [{"type": "command", "command": "echo read"}]},
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "j-cli _hooks notebook-exec-guard",
                             "_jcli_managed": "notebook-exec-guard"},
                        ],
                    },
                ]
            }
        }
        target = claude_dir / "settings.local.json"
        target.write_text(json.dumps(existing), encoding="utf-8")

        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0

        settings = _read_json(target)
        pre = settings.get("hooks", {}).get("PreToolUse", [])
        assert not any(b.get("hooks") == [] for b in pre)
        assert not any(b.get("matcher") == "Bash" for b in pre)
        assert any(b.get("matcher") == "Read" for b in pre)
