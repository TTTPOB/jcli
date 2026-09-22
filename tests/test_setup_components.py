"""Cross-host setup component selection and local-scope tests."""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.setup.common import is_git_tracked


def invoke(host: str, *args: str):
    return CliRunner().invoke(main, ["setup", host, *args], catch_exceptions=False)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / "agents-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return tmp_path


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_skill_only_does_not_invoke_mcp_or_create_hooks(isolated, monkeypatch, host):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("skill-only setup invoked MCP integration")

    monkeypatch.setattr(
        f"jupyter_jcli.commands.setup.hooks.manage_{host}_mcp", unexpected
    )
    result = invoke(host, "--only", "skill")

    assert result.exit_code == 0
    skill_root = ".claude" if host == "claude" else ".agents"
    assert (isolated / skill_root / "skills" / "j-cli" / "SKILL.md").exists()
    assert not (isolated / ".claude" / "settings.local.json").exists()
    assert not (isolated / ".codex" / "hooks.json").exists()


def test_repeatable_only_installs_exact_claude_components(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "jupyter_jcli.commands.setup.hooks.manage_claude_mcp",
        lambda *args, **kwargs: calls.append(args),
    )
    result = invoke("claude", "--only", "skill", "--only", "hook")

    assert result.exit_code == 0
    assert calls == []
    assert (isolated / ".claude" / "skills" / "j-cli" / "SKILL.md").exists()
    assert (isolated / ".claude" / "settings.local.json").exists()


def test_skill_dir_is_root_and_local_ignore_is_exact(isolated):
    result = invoke("opencode", "--only", "skill", "--skill-dir", "custom-skills")

    assert result.exit_code == 0
    target = isolated / "custom-skills" / "j-cli"
    assert (target / "SKILL.md").exists()
    ignore = (isolated / "custom-skills" / ".gitignore").read_text(encoding="utf-8")
    assert "/j-cli/" in ignore
    assert "custom-skills" not in ignore
    assert not (isolated / ".opencode").exists()


def test_project_switch_removes_only_selected_managed_ignore(isolated):
    assert invoke("opencode", "--only", "hook", "--only", "tool").exit_code == 0
    ignore_path = isolated / ".opencode" / ".gitignore"
    ignore_path.write_text(
        "# user\n" + ignore_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = invoke("opencode", "--project", "--only", "hook")

    assert result.exit_code == 0
    ignore = ignore_path.read_text(encoding="utf-8")
    assert "# user" in ignore
    assert "j-cli local hook" not in ignore
    assert "j-cli local tool" in ignore
    assert "/plugins/jcli.js" in ignore


def test_local_rejects_tracked_target_before_writes(isolated):
    subprocess.run(["git", "init", "-q"], cwd=isolated, check=True)
    target = isolated / ".opencode" / "plugins" / "jcli.js"
    target.parent.mkdir(parents=True)
    target.write_text("// tracked user file\n", encoding="utf-8")
    subprocess.run(["git", "add", str(target)], cwd=isolated, check=True)

    result = invoke("opencode", "--only", "hook")

    assert result.exit_code == 1
    assert "LOCAL_TARGET_TRACKED" in result.stderr
    assert target.read_text(encoding="utf-8") == "// tracked user file\n"
    assert not (isolated / ".opencode" / ".gitignore").exists()


def test_opencode_preserves_other_plugin_capability_on_remove(isolated):
    assert invoke("opencode", "--only", "hook").exit_code == 0
    assert invoke("opencode", "--only", "tool").exit_code == 0
    plugin = isolated / ".opencode" / "plugins" / "jcli.js"

    removed = invoke("opencode", "--remove", "--only", "hook")

    assert removed.exit_code == 0
    assert plugin.exists()
    assert 'const enabledCapabilities = {"hook":false,"tool":true}' in plugin.read_text(
        encoding="utf-8"
    )
    assert invoke("opencode", "--remove", "--only", "tool").exit_code == 0
    assert not plugin.exists()


def test_dsh_preserves_other_plugin_capability_on_remove(isolated):
    assert invoke("dsh", "--only", "hook").exit_code == 0
    assert invoke("dsh", "--only", "tool").exit_code == 0
    config = isolated / ".dsh" / "cordis.yml"
    plugin = isolated / ".dsh" / "plugins" / "jcli.ts"

    removed = invoke("dsh", "--remove", "--only", "hook")

    assert removed.exit_code == 0
    assert plugin.exists()
    text = config.read_text(encoding="utf-8")
    assert "hooks: false" in text
    assert "tools: true" in text
    assert invoke("dsh", "--remove", "--only", "tool").exit_code == 0
    assert not plugin.exists()


def test_user_skill_locations_respect_platform_environments(isolated):
    for host in ("codex", "dsh"):
        result = invoke(host, "--user", "--only", "skill")
        assert result.exit_code == 0

    assert (isolated / "codex-home" / "skills" / "j-cli" / "SKILL.md").exists()
    assert (isolated / "agents-home" / "skills" / "j-cli" / "SKILL.md").exists()


def test_skill_dir_requires_skill_selection(isolated):
    result = invoke("opencode", "--only", "hook", "--skill-dir", "elsewhere")
    assert result.exit_code == 1
    assert "SKILL_DIR_WITHOUT_SKILL" in result.stderr
    assert not (isolated / ".opencode").exists()


def test_codex_user_hook_respects_codex_home(isolated):
    result = invoke("codex", "--user", "--only", "hook")
    assert result.exit_code == 0
    assert (isolated / "codex-home" / "hooks.json").exists()
    assert not (isolated / "home" / ".codex" / "hooks.json").exists()


def test_git_tracking_uses_target_repository_outside_cwd(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "config" / "managed.json"
    target.parent.mkdir()
    target.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", str(target)], cwd=tmp_path, check=True)
    elsewhere = tmp_path / "nested" / "cwd"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    assert is_git_tracked(target)


def test_git_tracking_tolerates_missing_git(tmp_path, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert not is_git_tracked(tmp_path / "new" / "file")


def test_opencode_skill_only_ignores_broken_unselected_host_paths(isolated):
    (isolated / ".opencode" / "plugins" / "jcli.js").mkdir(parents=True)
    (isolated / ".opencode" / ".gitignore").mkdir()

    result = invoke("opencode", "--only", "skill")

    assert result.exit_code == 0
    assert (isolated / ".agents" / "skills" / "j-cli" / "SKILL.md").exists()


def test_opencode_rejects_damaged_explicit_capabilities(isolated):
    plugin = isolated / ".opencode" / "plugins" / "jcli.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(
        "// Managed by j-cli setup opencode.\nconst enabledCapabilities = {broken}\n",
        encoding="utf-8",
    )

    result = invoke("opencode", "--project", "--only", "hook")

    assert result.exit_code == 1
    assert "PLUGIN_CONFIG_INVALID" in result.stderr


def test_dsh_explicit_false_capabilities_do_not_enable_tool(isolated):
    config = isolated / ".dsh" / "cordis.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# >>> jcli managed (dsh hooks) >>>\n"
        "- id: jcli-hooks\n"
        "  name: './plugins/jcli.ts'\n"
        "  config:\n"
        "    hooks: false\n"
        "    tools: false\n"
        "# <<< jcli managed (dsh hooks) <<<\n",
        encoding="utf-8",
    )

    result = invoke("dsh", "--project", "--only", "hook")

    assert result.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "hooks: true" in text
    assert "tools: false" in text


def test_dsh_rejects_non_boolean_capability_flags(isolated):
    config = isolated / ".dsh" / "cordis.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# >>> jcli managed (dsh hooks) >>>\n"
        "- id: jcli-hooks\n"
        "  name: './plugins/jcli.ts'\n"
        "  config:\n"
        "    hooks: 'true'\n"
        "    tools: false\n"
        "# <<< jcli managed (dsh hooks) <<<\n",
        encoding="utf-8",
    )

    result = invoke("dsh", "--project", "--only", "hook")

    assert result.exit_code == 1
    assert "DSH_CONFIG_INVALID" in result.stderr
