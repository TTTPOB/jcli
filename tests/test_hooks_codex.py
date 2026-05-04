"""Tests for hook handlers with --platform codex (apply_patch input parsing)."""

import json

from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.hooks_cmd import (
    _extract_bash_command_codex,
    _extract_file_paths_codex,
    _parse_codex_apply_patch_file_paths,
)


class TestExtractBashCommandCodex:
    def test_string_command(self):
        payload = {"tool_input": {"command": "echo hello"}}
        assert _extract_bash_command_codex(payload) == "echo hello"

    def test_array_with_dash_c(self):
        payload = {
            "tool_input": {
                "command": ["bash", "-c", "jupyter nbconvert --execute foo.ipynb"]
            }
        }
        assert (
            _extract_bash_command_codex(payload)
            == "jupyter nbconvert --execute foo.ipynb"
        )

    def test_array_without_dash_c(self):
        payload = {"tool_input": {"command": ["python", "script.py"]}}
        assert _extract_bash_command_codex(payload) == "script.py"

    def test_empty_payload(self):
        assert _extract_bash_command_codex({}) == ""

    def test_missing_command(self):
        assert _extract_bash_command_codex({"tool_input": {}}) == ""


class TestParseCodexApplyPatchFilePaths:
    def test_single_update_file(self):
        text = "*** Begin Patch\n*** Update File: foo.py\n@@ ... @@\n- old\n+ new\n*** End Patch"
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py"]

    def test_multiple_files(self):
        text = (
            "*** Begin Patch\n"
            "*** Update File: foo.py\n@@ ... @@\n- old\n+ new\n"
            "*** Add File: bar.py\n+print('hello')\n"
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py", "bar.py"]

    def test_delete_file(self):
        text = "*** Begin Patch\n*** Delete File: stale.py\n*** End Patch"
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["stale.py"]

    def test_content_line_not_matched(self):
        """Line with + prefix is content, not a directive."""
        text = (
            "*** Begin Patch\n"
            "*** Update File: real.py\n@@ ... @@\n"
            " *** Update File: fake.py\n"  # space prefix = context line
            "+*** Add File: also_fake.py\n"  # + prefix = added line
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["real.py"]


class TestExtractFilePathsCodex:
    def test_array_command(self):
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: foo.py\n@@ -1 +1 @@\n- old\n+ new\n",
                ]
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == ["foo.py"]

    def test_string_command_fallback(self):
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Update File: bar.py\n@@ -1 +1 @@\n- old\n+ new\n"
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == ["bar.py"]

    def test_no_command(self):
        assert _extract_file_paths_codex({}) == []


class TestCodexNotebookExecGuard:
    def test_denies_nbconvert_with_codex_bash_array(self):
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {
                "command": ["bash", "-c", "jupyter nbconvert --execute foo.ipynb"]
            },
            "hook_event_name": "PreToolUse",
            "model": "gpt-5",
            "permission_mode": "default",
            "session_id": "test",
            "tool_use_id": "t1",
            "transcript_path": None,
            "cwd": "/tmp",
            "turn_id": "t1",
        })
        result = runner.invoke(
            main,
            ["_hooks", "notebook-exec-guard", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestCodexPythonRunGuard:
    def test_denies_script_with_paired_ipynb(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a .py file and its paired .ipynb
        (tmp_path / "test.py").write_text("# %%\nprint('hello')\n", encoding="utf-8")
        (tmp_path / "test.ipynb").write_text("{}", encoding="utf-8")
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {
                "command": ["bash", "-c", "python test.py"]
            },
            "hook_event_name": "PreToolUse",
            "model": "gpt-5",
            "permission_mode": "default",
            "session_id": "test",
            "tool_use_id": "t1",
            "transcript_path": None,
            "cwd": str(tmp_path),
            "turn_id": "t1",
        })
        result = runner.invoke(
            main,
            ["_hooks", "python-run-guard", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestCodexPairDriftGuardPre:
    def test_extracts_path_from_apply_patch(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a .py file so path.exists() passes
        (tmp_path / "foo.py").write_text("# %%\nprint('hello')\n", encoding="utf-8")
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: foo.py\n@@ -1 +1 @@\n- old\n+ new\n",
                ]
            },
            "hook_event_name": "PreToolUse",
            "model": "gpt-5",
            "permission_mode": "default",
            "session_id": "test",
            "tool_use_id": "t1",
            "transcript_path": None,
            "cwd": str(tmp_path),
            "turn_id": "t1",
        })
        result = runner.invoke(
            main,
            ["_hooks", "pair-drift-guard-pre", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        # exits 0 with no output when no pair exists (no .ipynb to drift-detect)
        assert result.exit_code == 0


class TestCodexExtractFilePaths:
    def test_skips_non_apply_patch_tool(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "*** Update File: fake.py\n@@ -1 +1 @@\n- old\n+ new\n"
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == []

    def test_handles_missing_tool_name(self):
        payload = {
            "tool_input": {
                "command": ["apply_patch", "*** Update File: foo.py\n@@ ... @@\n- old\n+ new\n"]
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == []

    def test_strips_trailing_whitespace_from_paths(self):
        text = "*** Begin Patch\n*** Update File: foo.py  \n@@ ... @@\n- old\n+ new\n*** End Patch"
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py"]


class TestExtractBashCommandCodex:
    def test_dash_c_not_at_index_1(self):
        """-c flag with --norc before it."""
        payload = {
            "tool_input": {
                "command": ["bash", "--norc", "-c", "echo hello"]
            }
        }
        result = _extract_bash_command_codex(payload)
        assert result == "echo hello"
