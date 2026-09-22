"""Tests for j-cli setup codex."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.setup import mcp as setup_mcp


@pytest.fixture(autouse=True)
def _isolate_codex_mcp(monkeypatch):
    """Keep legacy hook tests from invoking the real Codex CLI."""
    monkeypatch.setattr(
        "jupyter_jcli.commands.setup.codex.manage_codex_mcp",
        lambda *args, **kwargs: "unchanged",
    )


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
    def test_project_is_default(self, tmp_path, monkeypatch):
        """Default writes to .codex/hooks.json (Codex only reads hooks.json)."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 0
        target = tmp_path / ".codex" / "hooks.json"
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
        settings = _read_json(tmp_path / ".codex" / "hooks.json")
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
        settings = _read_json(tmp_path / ".codex" / "hooks.json")
        commands = []
        for event_list in settings.get("hooks", {}).values():
            for block in event_list:
                for entry in block.get("hooks", []):
                    commands.append(entry.get("command", ""))
        assert len(commands) == 4
        assert all(" --platform codex" in command for command in commands)

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.json")
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
        target = tmp_path / ".codex" / "hooks.json"
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
        (codex_dir / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")
        runner = CliRunner()
        _invoke(runner, ["--local", "--remove"])
        settings = _read_json(codex_dir / "hooks.json")
        assert _count_hooks(settings) == 1
        entry = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        assert entry == {
            "type": "command",
            "command": "my-custom-hook",
            "_custom": "keep-me",
        }


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
            main,
            ["--json", "setup", "codex", "--local", "--remove"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"


class TestCodexMcp:
    def test_project_install_targets_project_config_with_explicit_root(
        self, tmp_path, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda command, cwd, config_dir, use_json: calls.append(
                (command, cwd, config_dir, use_json)
            ),
        )

        assert (
            setup_mcp.manage_codex_mcp("project", tmp_path, False, False) == "installed"
        )
        assert calls == [
            (
                [
                    "codex",
                    "mcp",
                    "add",
                    "jcli-notebook-output",
                    "--",
                    "j-cli",
                    "mcp",
                    "serve",
                    "--root",
                    str(tmp_path),
                ],
                tmp_path,
                tmp_path / ".codex",
                False,
            )
        ]

    def test_project_install_is_idempotent_and_preserves_toml(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        original = f"""# keep this comment
[features]
codex_hooks = true

[mcp_servers.other]
command = "other-server"

[mcp_servers.jcli-notebook-output]
command = "j-cli"
args = ["mcp", "serve", "--root", "{tmp_path}"]
"""
        config = config_dir / "config.toml"
        config.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            setup_mcp,
            "_read_codex_entry",
            lambda *args: {
                "type": "stdio",
                "command": "j-cli",
                "args": ["mcp", "serve", "--root", str(tmp_path)],
                "env": None,
            },
        )
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda *args, **kwargs: pytest.fail("Codex CLI should not be called"),
        )

        assert (
            setup_mcp.manage_codex_mcp("project", tmp_path, False, False) == "unchanged"
        )
        assert config.read_text(encoding="utf-8") == original

    def test_same_name_with_other_command_is_rejected(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        original = """[mcp_servers.jcli-notebook-output]
command = "unrelated-server"
args = []
"""
        config.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            setup_mcp,
            "_read_codex_entry",
            lambda *args: {"command": "unrelated-server", "args": []},
        )
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda *args, **kwargs: pytest.fail("Codex CLI should not be called"),
        )

        with pytest.raises(SystemExit):
            setup_mcp.manage_codex_mcp("project", tmp_path, False, False)

        assert config.read_text(encoding="utf-8") == original

    def test_remove_owned_entry_uses_target_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        original = f"""# user comment
[mcp_servers.other]
command = "keep-me"

[mcp_servers.jcli-notebook-output]
command = "j-cli"
args = ["mcp", "serve", "--root", "{tmp_path}"]
"""
        config.write_text(original, encoding="utf-8")
        calls = []
        monkeypatch.setattr(
            setup_mcp,
            "_read_codex_entry",
            lambda *args: {
                "type": "stdio",
                "command": "j-cli",
                "args": ["mcp", "serve", "--root", str(tmp_path)],
            },
        )
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda command, cwd, target, use_json: calls.append((command, cwd, target)),
        )

        assert setup_mcp.manage_codex_mcp("project", tmp_path, True, False) == "removed"
        assert calls == [
            (
                ["codex", "mcp", "remove", "jcli-notebook-output"],
                tmp_path,
                config_dir,
            )
        ]
        # j-cli itself never rewrites TOML; Codex CLI owns the mutation.
        assert config.read_text(encoding="utf-8") == original

    def test_lookup_uses_structured_codex_output_in_target_home(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("# existing\n", encoding="utf-8")
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return setup_mcp.subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "name": "jcli-notebook-output",
                        "transport": {
                            "type": "stdio",
                            "command": "j-cli",
                            "args": ["mcp", "serve"],
                            "env": None,
                        },
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr(setup_mcp.subprocess, "run", fake_run)

        entry = setup_mcp._read_codex_entry(config_dir, tmp_path, False)

        assert entry["command"] == "j-cli"
        command, kwargs = calls[0]
        assert command == [
            "codex",
            "mcp",
            "get",
            "jcli-notebook-output",
            "--json",
        ]
        assert kwargs["env"]["CODEX_HOME"] == str(config_dir)
        assert kwargs["cwd"] == tmp_path

    def test_user_scope_does_not_bind_setup_cwd(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        calls = []
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda command, cwd, config_dir, use_json: calls.append(
                (command, config_dir)
            ),
        )

        setup_mcp.manage_codex_mcp("user", tmp_path, False, False)

        command, config_dir = calls[0]
        assert command[-3:] == ["j-cli", "mcp", "serve"]
        assert "--root" not in command
        assert config_dir == home / ".codex"
