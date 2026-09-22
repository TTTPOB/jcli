"""Cross-host setup component selection and local-scope tests."""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.setup import mcp as setup_mcp
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
        f"jupyter_jcli.commands.setup.{host}.manage_{host}_mcp", unexpected
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
        "jupyter_jcli.commands.setup.claude.manage_claude_mcp",
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


@pytest.mark.parametrize("host", ["claude", "codex", "dsh", "opencode"])
def test_force_is_allowed_without_selecting_skill(isolated, host):
    result = invoke(host, "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    assert not (isolated / ".agents" / "skills" / "j-cli").exists()


@pytest.mark.parametrize("host", ["claude", "codex", "dsh", "opencode"])
def test_force_remove_is_rejected_before_writes(isolated, host):
    result = invoke(host, "--only", "hook", "--force", "--remove")

    assert result.exit_code == 1
    assert "FORCE_WITH_REMOVE" in result.stderr
    assert not (isolated / ".claude").exists()
    assert not (isolated / ".codex").exists()
    assert not (isolated / ".dsh").exists()
    assert not (isolated / ".opencode").exists()


@pytest.mark.parametrize(
    ("host", "plugin_relative", "capability_text"),
    [
        ("dsh", ".dsh/plugins/jcli.ts", "tools: false"),
        ("opencode", ".opencode/plugins/jcli.js", '"tool":false'),
    ],
)
def test_force_replaces_foreign_plugin_symlink_without_writing_source(
    isolated, host, plugin_relative, capability_text
):
    source = isolated / "external-plugin"
    source.write_text("user-owned\n", encoding="utf-8")
    plugin = isolated / plugin_relative
    plugin.parent.mkdir(parents=True)
    plugin.symlink_to(source)

    result = invoke(host, "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    assert source.read_text(encoding="utf-8") == "user-owned\n"
    assert not plugin.is_symlink()
    if host == "dsh":
        rendered = (isolated / ".dsh" / "cordis.yml").read_text(encoding="utf-8")
    else:
        rendered = plugin.read_text(encoding="utf-8")
    assert capability_text in rendered


@pytest.mark.parametrize(
    ("host", "plugin_relative"),
    [
        ("dsh", ".dsh/plugins/jcli.ts"),
        ("opencode", ".opencode/plugins/jcli.js"),
    ],
)
def test_force_replaces_managed_plugin_symlink_even_when_content_matches(
    isolated, host, plugin_relative
):
    assert invoke(host, "--project", "--only", "hook").exit_code == 0
    plugin = isolated / plugin_relative
    source = isolated / f"{host}-managed-source"
    source.write_bytes(plugin.read_bytes())
    plugin.unlink()
    plugin.symlink_to(source)
    source_before = source.read_bytes()

    result = invoke(host, "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    assert not plugin.is_symlink()
    assert source.read_bytes() == source_before


@pytest.mark.parametrize(
    ("host", "hook_relative"),
    [
        ("claude", ".claude/settings.json"),
        ("codex", ".codex/hooks.json"),
    ],
)
def test_force_replaces_hook_symlink_without_writing_source(
    isolated, host, hook_relative
):
    source = isolated / f"{host}-external-hooks.json"
    source.write_text("{}\n", encoding="utf-8")
    hook_path = isolated / hook_relative
    hook_path.parent.mkdir(parents=True)
    hook_path.symlink_to(source)

    result = invoke(host, "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    assert source.read_text(encoding="utf-8") == "{}\n"
    assert not hook_path.is_symlink()
    assert "notebook-exec-guard" in hook_path.read_text(encoding="utf-8")


def test_dsh_force_foreign_plugin_does_not_inherit_managed_capabilities(isolated):
    assert invoke("dsh", "--project", "--only", "hook", "--only", "tool").exit_code == 0
    plugin = isolated / ".dsh" / "plugins" / "jcli.ts"
    plugin.write_text("foreign plugin\n", encoding="utf-8")

    result = invoke("dsh", "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    config = (isolated / ".dsh" / "cordis.yml").read_text(encoding="utf-8")
    assert "hooks: true" in config
    assert "tools: false" in config


def test_dsh_force_takes_over_reserved_row_and_preserves_other_yaml(isolated):
    config = isolated / ".dsh" / "cordis.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# keep\n"
        "- id: jcli-hooks\n"
        "  name: someone-else\n"
        "- id: other\n"
        "  name: keep-me\n",
        encoding="utf-8",
    )

    result = invoke("dsh", "--project", "--only", "tool", "--force")

    assert result.exit_code == 0
    rendered = config.read_text(encoding="utf-8")
    assert rendered.count("id: jcli-hooks") == 1
    assert "# keep" in rendered
    assert "id: other" in rendered
    assert "hooks: false" in rendered
    assert "tools: true" in rendered


@pytest.mark.parametrize("value", ["keep-me", "!!js 'keep-me'"])
def test_dsh_force_takes_over_reserved_row_in_flow_sequence(isolated, value):
    config = isolated / ".dsh" / "cordis.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "[{id: jcli-hooks, name: someone-else}, {id: other, name: " + value + "}]\n",
        encoding="utf-8",
    )

    result = invoke("dsh", "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    rendered = config.read_text(encoding="utf-8")
    assert rendered.count("id: jcli-hooks") == 1
    assert "id: other" in rendered
    assert "keep-me" in rendered
    assert ("!!js" in rendered) == value.startswith("!!js")


def test_force_hook_merge_preserves_user_hook(isolated):
    settings = isolated / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [{"type": "command", "command": "keep-me"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = invoke("claude", "--project", "--only", "hook", "--force")

    assert result.exit_code == 0
    rendered = json.loads(settings.read_text(encoding="utf-8"))
    assert rendered["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "keep-me"


@pytest.mark.parametrize("platform", ["claude", "codex"])
def test_mcp_force_removes_only_conflicting_name_then_adds(
    isolated, monkeypatch, platform
):
    calls = []
    monkeypatch.setattr(
        setup_mcp,
        f"_read_{platform}_entry",
        lambda *_args: {"command": "someone-else", "args": []},
    )
    if platform == "claude":
        monkeypatch.setattr(
            setup_mcp,
            "_run_claude",
            lambda command, *_args: calls.append(command),
        )
        result = setup_mcp.manage_claude_mcp(
            "project", isolated, False, False, force=True
        )
    else:
        monkeypatch.setattr(
            setup_mcp,
            "_run_codex",
            lambda command, *_args: calls.append(command),
        )
        result = setup_mcp.manage_codex_mcp(
            "project", isolated, False, False, force=True
        )

    assert result == "installed"
    assert calls[0][1:3] == ["mcp", "remove"]
    assert calls[1][1:3] == ["mcp", "add"]
    assert all("jcli-notebook-output" in command for command in calls)


def test_invalid_selected_global_config_warns_but_unselected_does_not(isolated):
    global_settings = isolated / "home" / ".claude" / "settings.json"
    global_settings.parent.mkdir(parents=True)
    global_settings.write_text("{broken", encoding="utf-8")

    skill_only = invoke("claude", "--project", "--only", "skill")
    hook_only = invoke("claude", "--project", "--only", "hook")

    assert skill_only.exit_code == 0
    assert str(global_settings) not in skill_only.stderr
    assert hook_only.exit_code == 0
    assert "could not inspect global hook integration" in hook_only.stderr
    assert str(global_settings) in hook_only.stderr
    assert global_settings.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("host", ["dsh", "opencode"])
def test_global_managed_plugin_warns_only_for_enabled_capability(isolated, host):
    assert invoke(host, "--user", "--only", "tool").exit_code == 0

    hook = invoke(host, "--project", "--only", "hook")
    tool = invoke(host, "--project", "--only", "tool")

    assert hook.exit_code == 0
    assert "global hook integration exists" not in hook.stderr
    assert tool.exit_code == 0
    assert tool.stderr.count("global tool integration exists") == 1


def test_force_local_skill_symlink_warns_and_does_not_modify_global(isolated):
    assert invoke("opencode", "--user", "--only", "skill").exit_code == 0
    global_skill = isolated / "home" / ".agents" / "skills" / "j-cli"
    local_skill = isolated / ".agents" / "skills" / "j-cli"
    local_skill.parent.mkdir(parents=True)
    local_skill.symlink_to(global_skill, target_is_directory=True)
    global_before = (global_skill / "SKILL.md").read_bytes()

    result = invoke("opencode", "--project", "--only", "skill", "--force")

    assert result.exit_code == 0
    assert str(global_skill) in result.stderr
    assert not local_skill.is_symlink()
    assert (global_skill / "SKILL.md").read_bytes() == global_before


def test_selected_global_conflict_warns_without_modifying_global(isolated):
    global_plugin = isolated / "home" / ".config" / "opencode" / "plugins" / "jcli.js"
    global_plugin.parent.mkdir(parents=True)
    global_plugin.write_text("global-user-plugin\n", encoding="utf-8")
    global_skill = isolated / "home" / ".agents" / "skills" / "j-cli"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("global skill\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["--json", "setup", "opencode", "--project", "--only", "hook", "--force"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "ok"
    assert str(global_plugin) in result.stderr
    assert str(global_skill) not in result.stderr
    assert global_plugin.read_text(encoding="utf-8") == "global-user-plugin\n"
    assert (global_skill / "SKILL.md").read_text(encoding="utf-8") == "global skill\n"
