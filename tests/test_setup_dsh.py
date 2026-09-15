"""Tests for `j-cli setup dsh`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from jupyter_jcli.cli import main

_MARKER = "# >>> jcli managed (dsh hooks) >>>"
_END = "# <<< jcli managed (dsh hooks) <<<"


@pytest.fixture(autouse=True)
def isolated_dsh_home(tmp_path, monkeypatch):
    """Keep every DSH setup test away from the real user profile."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "dsh"] + args, catch_exceptions=False)


def _workspace_paths(root: Path) -> tuple[Path, Path]:
    return root / ".dsh" / "cordis.yml", root / ".dsh" / "jcli-hooks.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _managed_entries(settings: dict) -> list[dict]:
    return [
        entry
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for entry in group.get("hooks", [])
        if entry.get("_jcli_managed") is not None
    ]


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


def test_default_local_alias_writes_workspace_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke(CliRunner(), [])

    assert result.exit_code == 0
    config, hooks = _workspace_paths(tmp_path)
    assert config.exists()
    assert hooks.exists()
    assert "no separate local layer" in result.stderr
    assert _MARKER in config.read_text()
    assert _END in config.read_text()
    document = _compose_one(config.read_text())
    assert isinstance(document, SequenceNode)
    row = _mapping(document.value[0])
    assert _scalar(row["id"]) == "jcli-hooks"
    row_config = _mapping(row["config"])
    assert _scalar(row_config["configPath"]) == str(hooks)
    entries = _managed_entries(_read_json(hooks))
    assert len(entries) == 4
    assert all(" --platform dsh" in entry["command"] for entry in entries)


def test_project_aliases_and_local_share_workspace_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    project = _invoke(runner, ["--proj"])
    assert project.exit_code == 0
    assert 'scope": "project"' not in project.output
    config, hooks = _workspace_paths(tmp_path)
    first_config = config.read_bytes()
    first_hooks = hooks.read_bytes()

    second = _invoke(runner, ["--project"])
    assert second.exit_code == 0
    assert config.read_bytes() == first_config
    assert hooks.read_bytes() == first_hooks
    assert "already up to date" in second.output


def test_global_and_user_alias_use_dsh_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dsh_home = tmp_path / "custom-dsh"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))

    result = _invoke(CliRunner(), ["--user"])

    assert result.exit_code == 0
    config = dsh_home / "cordis.patch.yml"
    hooks = dsh_home / "jcli-hooks.json"
    assert config.exists()
    assert hooks.exists()
    text = config.read_text()
    document = _compose_one(text)
    assert isinstance(document, SequenceNode)
    patch = _mapping(document.value[0])
    inserted = patch["insert"]
    assert isinstance(inserted, SequenceNode)
    row = _mapping(inserted.value[0])
    assert _scalar(row["id"]) == "jcli-hooks"
    row_config = _mapping(row["config"])
    assert _scalar(row_config["configPath"]) == str(hooks)


@pytest.mark.parametrize(
    ("args", "config_name"),
    [(["--project"], "cordis.yml"), (["--global"], "cordis.patch.yml")],
)
def test_empty_flow_sequence_is_replaced_and_parsed(
    tmp_path, monkeypatch, args, config_name
):
    monkeypatch.chdir(tmp_path)
    dsh_home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    config = (
        (tmp_path / ".dsh" / config_name)
        if config_name == "cordis.yml"
        else dsh_home / config_name
    )
    config.parent.mkdir(parents=True)
    config.write_text("# before\n[]\n# after\n", encoding="utf-8")

    result = _invoke(CliRunner(), args)

    assert result.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "# before" in text and "# after" in text
    document = _compose_one(text)
    assert isinstance(document, SequenceNode)
    assert len(document.value) == 1


@pytest.mark.parametrize("initial", ["---\n[]\n", "--- [] # comment\n"])
@pytest.mark.parametrize(
    ("args", "config_name"),
    [(["--project"], "cordis.yml"), (["--global"], "cordis.patch.yml")],
)
def test_empty_document_full_install_idempotence_remove_cycle(
    tmp_path, monkeypatch, initial, args, config_name
):
    monkeypatch.chdir(tmp_path)
    dsh_home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    config = (
        (tmp_path / ".dsh" / config_name)
        if config_name == "cordis.yml"
        else dsh_home / config_name
    )
    config.parent.mkdir(parents=True)
    config.write_text(initial, encoding="utf-8")
    runner = CliRunner()

    first = _invoke(runner, args)
    assert first.exit_code == 0
    installed = config.read_bytes()
    second = _invoke(runner, args)
    assert second.exit_code == 0
    assert config.read_bytes() == installed

    removed = _invoke(runner, [*args, "--remove"])
    assert removed.exit_code == 0
    assert _MARKER not in config.read_text(encoding="utf-8")
    assert isinstance(_compose_one(config.read_text(encoding="utf-8")), SequenceNode)


def test_empty_dsh_home_uses_default_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DSH_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = _invoke(CliRunner(), ["--global"])

    assert result.exit_code == 0
    assert (tmp_path / ".dsh" / "cordis.patch.yml").exists()
    assert (tmp_path / ".dsh" / "jcli-hooks.json").exists()


def test_preserves_yaml_comments_tags_and_user_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
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
    assert "configPath: '" in generated


def test_install_replaces_stale_managed_block_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, _ = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "# before\n"
        "- id: before\n"
        "  name: custom\n"
        f"{_MARKER}\n"
        "- id: jcli-hooks\n"
        "  name: old\n"
        f"{_END}\n"
        "- id: after\n"
        "  name: custom\n"
        f"{_MARKER}\n"
        "- id: jcli-hooks\n"
        "  name: duplicate\n"
        f"{_END}\n",
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
    assert "@deepseek-ai/dsh-hooks-claude-code" in generated


def test_install_upgrades_stale_dsh_matchers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, hooks = _workspace_paths(tmp_path)
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old",
                                    "_jcli_managed": "notebook-exec-guard",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    settings = _read_json(hooks)
    managed = _managed_entries(settings)
    assert len(managed) == 4
    assert all(entry["command"].endswith("--platform dsh") for entry in managed)
    assert all(group["matcher"] != "Bash" for group in settings["hooks"]["PreToolUse"])


def test_warns_when_workspace_and_global_rows_are_both_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dsh_home = tmp_path / "home-dsh"
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    runner = CliRunner()

    assert _invoke(runner, ["--project"]).exit_code == 0
    result = _invoke(runner, ["--global"])

    assert result.exit_code == 0
    assert "both scopes" in result.stderr


def test_invalid_yaml_fails_before_creating_hooks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, hooks = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("- id: [broken\n", encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_CONFIG_INVALID" in result.stderr
    assert not hooks.exists()
    assert config.read_text(encoding="utf-8") == "- id: [broken\n"


def test_unmanaged_row_id_conflict_fails_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, hooks = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    original = "- id: jcli-hooks\n  name: someone-else\n"
    config.write_text(original, encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_CONFIG_CONFLICT" in result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert not hooks.exists()


def test_remove_preserves_unrelated_config_and_is_noop_afterwards(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert _invoke(runner, ["--project"]).exit_code == 0
    config, hooks = _workspace_paths(tmp_path)
    config.write_text(
        "# user-owned\n- id: user-row\n  name: custom\n"
        + config.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    removed = _invoke(runner, ["--project", "--remove"])
    assert removed.exit_code == 0
    assert "user-owned" in config.read_text(encoding="utf-8")
    assert _MARKER not in config.read_text(encoding="utf-8")
    assert not hooks.exists()

    noop = _invoke(runner, ["--project", "--remove"])
    assert noop.exit_code == 0
    assert "Nothing to remove" in noop.output


def test_remove_keeps_comments_as_valid_empty_sequence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, _ = _workspace_paths(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text(
        "# keep this comment\n" + _MARKER + "\n- id: jcli-hooks\n" + _END + "\n",
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project", "--remove"])

    assert result.exit_code == 0
    text = config.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert _compose_one(text) is not None
    assert text.rstrip().endswith("[]")


def test_invalid_hook_json_fails_before_any_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, hooks = _workspace_paths(tmp_path)
    hooks.parent.mkdir(parents=True)
    original = "{not-json"
    hooks.write_text(original, encoding="utf-8")

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 1
    assert "DSH_HOOKS_INVALID" in result.stderr
    assert not config.exists()
    assert hooks.read_text(encoding="utf-8") == original


def test_preserves_user_hook_json_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, hooks = _workspace_paths(tmp_path)
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "custom": {"keep": True},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "custom",
                            "hooks": [{"type": "command", "command": "user-hook"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(CliRunner(), ["--project"])

    assert result.exit_code == 0
    settings = _read_json(hooks)
    assert settings["custom"] == {"keep": True}
    assert any(
        entry.get("command") == "user-hook"
        for group in settings["hooks"]["PreToolUse"]
        for entry in group["hooks"]
    )


def test_malformed_dsh_payload_fails_open(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    payload = {"cwd": 42, "tool_input": None}

    result = runner.invoke(
        main,
        ["_hooks", "notebook-exec-guard", "--platform", "dsh"],
        input=json.dumps(payload),
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.stdout == ""


def test_dsh_hooks_match_lowercase_tool_names_and_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert _invoke(runner, []).exit_code == 0

    bash_payload = {
        "cwd": str(tmp_path),
        "tool_name": "bash",
        "tool_input": {"command": "jupyter nbconvert --execute note.ipynb"},
    }
    denied = runner.invoke(
        main,
        ["_hooks", "notebook-exec-guard", "--platform", "dsh"],
        input=json.dumps(bash_payload),
        catch_exceptions=False,
    )
    assert denied.exit_code == 0
    assert (
        json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )

    subdir = tmp_path / "session"
    subdir.mkdir()
    (subdir / "script.py").write_text("print(1)\n", encoding="utf-8")
    (subdir / "script.ipynb").write_text("{}\n", encoding="utf-8")
    python_payload = {
        "cwd": str(tmp_path),
        "tool_name": "bash",
        "tool_input": {"command": "python script.py", "workdir": "session"},
    }
    python_denied = runner.invoke(
        main,
        ["_hooks", "python-run-guard", "--platform", "dsh"],
        input=json.dumps(python_payload),
        catch_exceptions=False,
    )
    assert python_denied.exit_code == 0
    assert (
        json.loads(python_denied.stdout)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )

    edit_payload = {
        "cwd": str(tmp_path),
        "tool_name": "edit",
        "tool_input": {"file_path": "note.ipynb"},
    }
    edit_denied = runner.invoke(
        main,
        ["_hooks", "pair-drift-guard-pre", "--platform", "dsh"],
        input=json.dumps(edit_payload),
        catch_exceptions=False,
    )
    assert edit_denied.exit_code == 0
    assert (
        json.loads(edit_denied.stdout)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )
