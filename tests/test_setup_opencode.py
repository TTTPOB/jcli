"""Tests for j-cli setup opencode."""

import json
from importlib import resources

from click.testing import CliRunner

from jupyter_jcli.cli import main


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "opencode"] + args, catch_exceptions=False)


class TestOpenCodeScopeRouting:
    def test_project_is_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(CliRunner(), [])
        assert result.exit_code == 0
        assert (tmp_path / ".opencode" / "plugins" / "jcli.js").exists()

    def test_user_writes_global_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _invoke(CliRunner(), ["--user"])
        assert result.exit_code == 0
        assert (tmp_path / ".config" / "opencode" / "plugins" / "jcli.js").exists()

    def test_local_is_project_alias(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(CliRunner(), ["--local"])
        assert result.exit_code == 0
        assert "no local plugin layer" in result.stderr
        assert (tmp_path / ".opencode" / "plugins" / "jcli.js").exists()


class TestOpenCodeInstall:
    def test_installs_packaged_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(CliRunner(), [])
        assert result.exit_code == 0
        installed = (tmp_path / ".opencode" / "plugins" / "jcli.js").read_text(
            encoding="utf-8"
        )
        packaged = (
            resources.files("jupyter_jcli")
            .joinpath("opencode_plugin.js")
            .read_text(encoding="utf-8")
        )
        assert installed == packaged

    def test_second_install_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        assert _invoke(runner, []).exit_code == 0
        result = _invoke(runner, [])
        assert result.exit_code == 0
        assert "already up to date" in result.stdout

    def test_updates_stale_managed_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / ".opencode" / "plugins" / "jcli.js"
        target.parent.mkdir(parents=True)
        target.write_text(
            "// Managed by j-cli setup opencode.\n// stale\n", encoding="utf-8"
        )
        result = _invoke(CliRunner(), [])
        assert result.exit_code == 0
        assert "export const JcliPlugin" in target.read_text(encoding="utf-8")

    def test_refuses_to_overwrite_foreign_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / ".opencode" / "plugins" / "jcli.js"
        target.parent.mkdir(parents=True)
        target.write_text("export const Mine = {}\n", encoding="utf-8")
        result = _invoke(CliRunner(), [])
        assert result.exit_code == 1
        assert "PLUGIN_CONFLICT" in result.stderr
        assert target.read_text(encoding="utf-8") == "export const Mine = {}\n"

    def test_warns_when_other_scope_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        assert _invoke(runner, []).exit_code == 0
        result = _invoke(runner, ["--user"])
        assert result.exit_code == 0
        assert "will load both" in result.stderr


class TestOpenCodeRemove:
    def test_removes_managed_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        assert _invoke(runner, []).exit_code == 0
        target = tmp_path / ".opencode" / "plugins" / "jcli.js"
        result = _invoke(runner, ["--remove"])
        assert result.exit_code == 0
        assert not target.exists()

    def test_missing_plugin_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(CliRunner(), ["--remove"])
        assert result.exit_code == 0
        assert "does not exist" in result.stdout

    def test_refuses_to_remove_foreign_plugin(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / ".opencode" / "plugins" / "jcli.js"
        target.parent.mkdir(parents=True)
        target.write_text("export const Mine = {}\n", encoding="utf-8")
        result = _invoke(CliRunner(), ["--remove"])
        assert result.exit_code == 1
        assert "PLUGIN_NOT_MANAGED" in result.stderr
        assert target.exists()


class TestOpenCodeJsonOutput:
    def test_install_and_remove_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        install = runner.invoke(
            main, ["--json", "setup", "opencode"], catch_exceptions=False
        )
        assert install.exit_code == 0
        assert json.loads(install.stdout)["status"] == "ok"

        remove = runner.invoke(
            main,
            ["--json", "setup", "opencode", "--remove"],
            catch_exceptions=False,
        )
        assert remove.exit_code == 0
        assert json.loads(remove.stdout)["status"] == "ok"
