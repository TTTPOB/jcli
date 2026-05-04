"""Tests for j-cli setup codex."""

import json
from pathlib import Path

from click.testing import CliRunner

from jupyter_jcli.cli import main


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "codex"] + args, catch_exceptions=False)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count_hooks(settings: dict) -> int:
    total = 0
    for event_list in settings.get("hooks", {}).values():
        for block in event_list:
            total += len(block.get("hooks", []))
    return total


class TestCodexScopeRouting:
    def test_local_is_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 0
        target = tmp_path / ".codex" / "hooks.local.json"
        assert target.exists()

    def test_project_writes_settings_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--project"])
        assert result.exit_code == 0
        target = tmp_path / ".codex" / "hooks.json"
        assert target.exists()

    def test_user_writes_home(self, tmp_path, monkeypatch):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = _invoke(runner, ["--user"])
        assert result.exit_code == 0
        assert (codex_dir / "hooks.json").exists()


class TestCodexInstall:
    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        assert not codex_dir.exists()
        runner = CliRunner()
        _invoke(runner, ["--local"])
        assert codex_dir.is_dir()

    def test_writes_four_guards_no_notebook_edit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        count = _count_hooks(settings)
        assert count == 4

        managed_vals = set()
        for event_list in settings.get("hooks", {}).values():
            for block in event_list:
                for entry in block.get("hooks", []):
                    managed_vals.add(entry.get("_jcli_managed"))
        assert "notebook-edit-guard" not in managed_vals

    def test_commands_have_platform_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        for event_list in settings.get("hooks", {}).values():
            for block in event_list:
                for entry in block.get("hooks", []):
                    assert " --platform codex" in entry.get("command", "")

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        assert _count_hooks(settings) == 4

    def test_warns_when_config_toml_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" in result.stderr

    def test_warns_when_feature_flag_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\nsome_other_flag = true\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" in result.stderr

    def test_no_warning_when_feature_flag_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\ncodex_hooks = true\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" not in result.stderr

    def test_warns_when_feature_flag_explicitly_false(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\ncodex_hooks = false\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" in result.stderr

    def test_no_warning_with_inline_comment(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\ncodex_hooks = true  # required by j-cli\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" not in result.stderr


class TestCodexRemove:
    def test_remove_cleans_all_entries(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        target = tmp_path / ".codex" / "hooks.local.json"
        assert target.exists()
        _invoke(runner, ["--local", "--remove"])
        assert not target.exists()

    def test_remove_noop_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0
        assert "does not exist" in result.stdout

    def test_remove_preserves_non_managed_hooks(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "my-custom-hook",
                                "_custom": "keep-me",
                            }
                        ],
                    }
                ]
            }
        }
        (codex_dir / "hooks.local.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )
        runner = CliRunner()
        _invoke(runner, ["--local", "--remove"])
        settings = _read_json(codex_dir / "hooks.local.json")
        assert _count_hooks(settings) == 1


class TestCodexJsonOutput:
    def test_install_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "codex", "--local"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"

    def test_remove_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = runner.invoke(
            main, ["--json", "setup", "codex", "--local", "--remove"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
