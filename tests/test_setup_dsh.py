"""Tests for ``j-cli setup dsh`` native adapter installation."""

from __future__ import annotations

import json
import stat
from importlib import metadata
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

import jupyter_jcli.commands.setup.dsh as dsh_module
from jupyter_jcli.cli import main

_MARKER = "# >>> jcli managed (dsh hooks) >>>"
_END = "# <<< jcli managed (dsh hooks) <<<"
_PLUGIN_MARKER = "// Managed by j-cli setup dsh."


@pytest.fixture(autouse=True)
def isolated_dsh_environment(tmp_path, monkeypatch):
    """Keep every DSH setup test away from the real home and cwd."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.chdir(tmp_path)


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "dsh"] + args, catch_exceptions=False)


def _workspace_paths(root: Path) -> tuple[Path, Path]:
    return root / ".dsh" / "cordis.yml", root / ".dsh" / "plugins" / "jcli.ts"


def _legacy_path(root: Path, global_scope: bool = False) -> Path:
    base = root / "dsh-home" if global_scope else root / ".dsh"
    return base / "jcli-hooks.json"


def _compose_one(text: str):
    documents = list(yaml.compose_all(text))
    assert len(documents) == 1
    return documents[0]


def _mapping(node: MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in node.value:
        assert isinstance(key, ScalarNode)
        result[key.value] = value
    return result


def _scalar(node: object) -> str:
    assert isinstance(node, ScalarNode)
    return node.value


def test_missing_resource_fails_before_creating_any_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        dsh_module,
        "_DSH_PLUGIN_RESOURCE",
        "missing-dsh-plugin.ts",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_PLUGIN_RESOURCE_MISSING" in result.stderr
    assert not (tmp_path / ".dsh").exists()


@pytest.mark.usefixtures("isolated_dsh_environment")
def test_native_resource_is_copied_with_generated_metadata(tmp_path):
    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    _, plugin = _workspace_paths(tmp_path)
    lines = plugin.read_text(encoding="utf-8").splitlines()
    assert lines[0] == _PLUGIN_MARKER
    assert lines[1] == f"// j-cli version {metadata.version('jupyter-jcli')}"
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o644


def test_damaged_resource_header_fails_before_creating_any_file(monkeypatch, tmp_path):
    class DamagedResource:
        def joinpath(self, _name):
            return self

        def read_text(self, encoding="utf-8"):
            return "// not managed by j-cli\\nexport const apply = () => {}\\n"

    monkeypatch.setattr(
        dsh_module.resources, "files", lambda _package: DamagedResource()
    )
    result = _invoke(CliRunner(), ["--project", "--only", "hook", "--only", "tool"])

    assert result.exit_code == 1
    assert "DSH_PLUGIN_INVALID" in result.stderr
    assert not (tmp_path / ".dsh").exists()


def test_default_local_alias_writes_workspace_native_files(tmp_path):
    result = _invoke(CliRunner(), [])

    assert result.exit_code == 0
    config, plugin = _workspace_paths(tmp_path)
    assert config.exists()
    assert plugin.exists()
    assert "no separate local layer" in result.stderr
    text = config.read_text(encoding="utf-8")
    assert _MARKER in text and _END in text
    document = _compose_one(text)
    assert isinstance(document, SequenceNode)
    row = _mapping(document.value[0])
    assert _scalar(row["id"]) == "jcli-hooks"
    assert _scalar(row["name"]) == "./plugins/jcli.ts"
    assert "configPath" not in text
    assert not _legacy_path(tmp_path).exists()


def test_project_aliases_and_local_share_workspace_target(tmp_path):
    runner = CliRunner()

    project = _invoke(runner, ["--proj"])
    assert project.exit_code == 0
    config, plugin = _workspace_paths(tmp_path)
    first_config = config.read_bytes()
    first_plugin = plugin.read_bytes()

    second = _invoke(runner, ["--project"])
    assert second.exit_code == 0
    assert config.read_bytes() == first_config
    assert plugin.read_bytes() == first_plugin
    assert "already up to date" in second.output


def test_global_and_user_alias_use_distinct_dsh_home_native_paths(
    tmp_path, monkeypatch
):
    dsh_home = tmp_path / "custom-dsh"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))

    result = _invoke(CliRunner(), ["--user"])

    assert result.exit_code == 0
    config = dsh_home / "cordis.patch.yml"
    plugin = dsh_home / "plugins" / "jcli.ts"
    assert config.exists() and plugin.exists()
    document = _compose_one(config.read_text(encoding="utf-8"))
    assert isinstance(document, SequenceNode)
    patch = _mapping(document.value[0])
    inserted = patch["insert"]
    assert isinstance(inserted, SequenceNode)
    row = _mapping(inserted.value[0])
    assert _scalar(row["id"]) == "jcli-hooks"
    assert _scalar(row["name"]) == str(plugin.resolve())
    assert "configPath" not in config.read_text(encoding="utf-8")

    first_config = config.read_bytes()
    first_plugin = plugin.read_bytes()
    alias = _invoke(CliRunner(), ["--global"])
    assert alias.exit_code == 0
    assert config.read_bytes() == first_config
    assert plugin.read_bytes() == first_plugin


def test_empty_flow_sequence_preserves_comments_and_parses(tmp_path):
    config, _ = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("# before\n[]\n# after\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "# before" in text and "# after" in text
    document = _compose_one(text)
    assert isinstance(document, SequenceNode)
    assert len(document.value) == 1


@pytest.mark.parametrize("initial", ["---\n[]\n", "--- [] # comment\n"])
def test_empty_document_install_idempotence_remove_cycle(tmp_path, initial):
    config, plugin = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(initial, encoding="utf-8")
    runner = CliRunner()

    first = _invoke(runner, ["--project"])
    assert first.exit_code == 0
    installed = config.read_bytes()
    second = _invoke(runner, ["--project"])
    assert second.exit_code == 0
    assert config.read_bytes() == installed
    assert plugin.exists()

    removed = _invoke(runner, ["--project", "--remove"])
    assert removed.exit_code == 0
    assert _MARKER not in config.read_text(encoding="utf-8")
    assert isinstance(_compose_one(config.read_text(encoding="utf-8")), SequenceNode)
    assert not plugin.exists()


def test_empty_dsh_home_uses_default_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = _invoke(CliRunner(), ["--global"])

    assert result.exit_code == 0
    assert (tmp_path / ".dsh" / "cordis.patch.yml").exists()
    assert (tmp_path / ".dsh" / "plugins" / "jcli.ts").exists()


def test_preserves_yaml_comments_tags_and_user_rows(tmp_path):
    config, _ = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    original = (
        "# keep this comment\n"
        "- id: user-row\n"
        "  name: custom\n"
        "  config:\n"
        "    expr: !!js process.cwd()\n"
    )
    config.write_text(original, encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    generated = config.read_text(encoding="utf-8")
    assert original in generated
    assert _MARKER in generated
    assert "name: './plugins/jcli.ts'" in generated
    assert "@deepseek-ai/dsh-hooks-claude-code" not in generated


def test_install_replaces_stale_managed_bridge_row_in_place(tmp_path):
    config, _ = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "# before\n"
        "- id: before\n"
        "  name: custom\n"
        f"{_MARKER}\n"
        "- id: jcli-hooks\n"
        "  name: '@deepseek-ai/dsh-hooks-claude-code'\n"
        "  config:\n"
        "    configPath: '/old/jcli-hooks.json'\n"
        f"{_END}\n"
        "- id: after\n"
        "  name: custom\n",
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    generated = config.read_text(encoding="utf-8")
    assert generated.count(_MARKER) == 1
    assert generated.count("id: jcli-hooks") == 1
    assert (
        generated.index("id: before")
        < generated.index(_MARKER)
        < generated.index("id: after")
    )
    assert "./plugins/jcli.ts" in generated
    assert "@deepseek-ai/dsh-hooks-claude-code" not in generated


def test_migrates_legacy_json_and_warns_about_user_content(tmp_path):
    config, _ = _workspace_paths(tmp_path)
    legacy = _legacy_path(tmp_path)
    config.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"{_MARKER}\n- id: jcli-hooks\n  name: old\n{_END}\n",
        encoding="utf-8",
    )
    legacy.write_text(
        json.dumps(
            {
                "custom": {"keep": True},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "^bash$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old",
                                    "_jcli_managed": "notebook-exec-guard",
                                },
                                {"type": "command", "command": "user-hook"},
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    cleaned = json.loads(legacy.read_text(encoding="utf-8"))
    assert cleaned["custom"] == {"keep": True}
    assert cleaned["hooks"]["PreToolUse"][0]["hooks"] == [
        {"type": "command", "command": "user-hook"}
    ]
    assert "does not execute custom legacy hooks" in result.stderr
    assert "configPath" not in config.read_text(encoding="utf-8")


def test_global_legacy_migration_keeps_user_content(tmp_path, monkeypatch):
    dsh_home = tmp_path / "global-dsh"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    config = dsh_home / "cordis.patch.yml"
    legacy = dsh_home / "jcli-hooks.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"{_MARKER}\n- insert:\n    - id: jcli-hooks\n      name: old\n{_END}\n",
        encoding="utf-8",
    )
    legacy.write_text(
        json.dumps(
            {
                "custom": True,
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"_jcli_managed": "notebook-exec-guard"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--user"])

    assert result.exit_code == 0
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"custom": True}
    assert "does not execute custom legacy hooks" in result.stderr
    assert str(dsh_home / "plugins" / "jcli.ts") in config.read_text(encoding="utf-8")


def test_empty_legacy_file_is_removed_as_migration_state(tmp_path):
    legacy = _legacy_path(tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text("  \n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    assert not legacy.exists()


def test_migrates_legacy_json_to_deletion_when_only_managed_entries(tmp_path):
    legacy = _legacy_path(tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "^bash$",
                            "hooks": [{"_jcli_managed": "notebook-exec-guard"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    assert not legacy.exists()


def test_warns_when_workspace_and_global_rows_are_both_present(tmp_path, monkeypatch):
    dsh_home = tmp_path / "home-dsh"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    runner = CliRunner()

    assert _invoke(runner, ["--project"]).exit_code == 0
    result = _invoke(runner, ["--global"])

    assert result.exit_code == 0
    assert "both scopes" in result.stderr


def test_invalid_yaml_fails_before_creating_plugin(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("- id: [broken\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_CONFIG_INVALID" in result.stderr
    assert not plugin.exists()
    assert config.read_text(encoding="utf-8") == "- id: [broken\n"


def test_invalid_legacy_json_fails_before_writing_plugin(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    legacy = _legacy_path(tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{not-json", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_HOOKS_INVALID" in result.stderr
    assert not config.exists()
    assert not plugin.exists()
    assert legacy.read_text(encoding="utf-8") == "{not-json"


def test_unmanaged_row_id_conflict_fails_without_writing(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    original = "- id: jcli-hooks\n  name: someone-else\n"
    config.write_text(original, encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_CONFIG_CONFLICT" in result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert not plugin.exists()


def test_nonmanaged_plugin_conflict_fails_without_touching_config(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const user = true;\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_PLUGIN_CONFLICT" in result.stderr
    assert plugin.read_text(encoding="utf-8") == "export const user = true;\n"
    assert not config.exists()


def test_nonmanaged_plugin_cannot_be_removed(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const user = true;\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project", "--remove"])

    assert result.exit_code == 1
    assert "DSH_PLUGIN_NOT_MANAGED" in result.stderr
    assert plugin.exists()
    assert not config.exists()


def test_atomic_plugin_write_failure_returns_error_and_no_row(tmp_path, monkeypatch):
    config, plugin = _workspace_paths(tmp_path)

    def fail_replace(*args, **kwargs):
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(dsh_module.os, "replace", fail_replace)
    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_WRITE_FAILED" in result.stderr
    assert not config.exists()
    assert not plugin.exists()
    assert (
        not list(plugin.parent.glob(".*.jcli.ts.*")) if plugin.parent.exists() else True
    )


def test_atomic_replacement_preserves_existing_plugin_permissions(tmp_path):
    _, plugin = _workspace_paths(tmp_path)
    assert _invoke(CliRunner(), ["--project"]).exit_code == 0

    plugin.chmod(0o640)
    plugin.write_text(
        plugin.read_text(encoding="utf-8") + "// stale\\n", encoding="utf-8"
    )
    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o640


def test_remove_preserves_unrelated_config_and_user_legacy_content(tmp_path):
    runner = CliRunner()
    assert _invoke(runner, ["--project"]).exit_code == 0
    config, plugin = _workspace_paths(tmp_path)
    legacy = _legacy_path(tmp_path)
    legacy.write_text(json.dumps({"custom": True}), encoding="utf-8")
    config.write_text(
        "# user-owned\n- id: user-row\n  name: custom\n"
        + config.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    removed = _invoke(runner, ["--project", "--remove"])

    assert removed.exit_code == 0
    assert "user-owned" in config.read_text(encoding="utf-8")
    assert _MARKER not in config.read_text(encoding="utf-8")
    assert not plugin.exists()
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"custom": True}
    assert "does not execute custom legacy hooks" in removed.stderr

    noop = _invoke(runner, ["--project", "--remove"])
    assert noop.exit_code == 0
    assert "Nothing to remove" in noop.output


def test_remove_keeps_comments_as_valid_empty_sequence(tmp_path):
    config, plugin = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "# keep this comment\n" + _MARKER + "\n- id: jcli-hooks\n" + _END + "\n",
        encoding="utf-8",
    )
    plugin.parent.mkdir(parents=True)
    plugin.write_text(_PLUGIN_MARKER + "\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project", "--remove"])

    assert result.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert _compose_one(text) is not None
    assert text.rstrip().endswith("[]")
    assert not plugin.exists()
