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

    def test_dash_c_not_at_index_1(self):
        """-c flag with --norc before it."""
        payload = {
            "tool_input": {
                "command": ["bash", "--norc", "-c", "echo hello"]
            }
        }
        result = _extract_bash_command_codex(payload)
        assert result == "echo hello"


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

    def test_rename_move_to(self):
        """Extracts both old (Update File) and new (Move to) paths."""
        text = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "*** Move to: src/main.py\n"
            "@@ -1 +1 @@\n"
            "-print('hi')\n"
            "+print('hello')\n"
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["src/app.py", "src/main.py"]

    def test_move_to_with_add_and_delete(self):
        """Move to works alongside Add and Delete hunks."""
        text = (
            "*** Begin Patch\n"
            "*** Update File: old/foo.py\n"
            "*** Move to: new/foo.py\n"
            "@@ ... @@\n"
            "*** Add File: new/bar.py\n"
            "+print('bar')\n"
            "*** Delete File: old/baz.py\n"
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["old/foo.py", "new/foo.py", "new/bar.py", "old/baz.py"]

    def test_legacy_underscore_asterisk_format(self):
        """Legacy ``**_ `` markers from the old system prompt are also matched."""
        text = (
            "**_ Begin Patch\n"
            "**_ Update File: foo.py\n"
            "**_ Move to: bar.py\n"
            "@@ -1 +1 @@\n"
            "- old\n"
            "+ new\n"
            "**_ Add File: baz.py\n"
            "+print('new')\n"
            "_** End Patch\n"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py", "bar.py", "baz.py"]


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


class TestCodexMultiFilePreMerge:
    """Multi-file apply_patch should merge multiple deny reasons into one DENY."""

    def test_multiple_ipynb_files_merged_deny(self, tmp_path, monkeypatch):
        """Multiple .ipynb files in one patch → single merged DENY."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    (
                        "*** Begin Patch\n"
                        "*** Update File: a.ipynb\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** Update File: b.ipynb\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** End Patch"
                    ),
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
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        # Both files should be mentioned in the merged reason
        assert "a.ipynb" in reason
        assert "b.ipynb" in reason
        # Separator present for multi-entry merge
        assert "---" in reason

    def test_ipynb_and_py_drift_merged_deny(self, tmp_path, monkeypatch):
        """Mix of .ipynb denial and .py drift denial → single merged DENY."""
        monkeypatch.chdir(tmp_path)
        # Create .py file so path.exists() passes, mock the drift check
        (tmp_path / "foo.py").write_text("# %%\nprint('hello')\n", encoding="utf-8")

        def fake_pre_drift_check(path, logger=None):
            return "Pre-existing drift detected for `foo.py` — resolve before editing."

        monkeypatch.setattr(
            "jupyter_jcli.commands.hooks_cmd._run_pre_drift_check",
            fake_pre_drift_check,
        )

        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    (
                        "*** Begin Patch\n"
                        "*** Update File: bar.ipynb\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** Update File: foo.py\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** End Patch"
                    ),
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
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        # Both the .ipynb denial and the .py drift denial should be present
        assert "bar.ipynb" in reason
        assert "foo.py" in reason
        assert "---" in reason

    def test_single_file_still_produces_deny(self, tmp_path, monkeypatch):
        """Single .ipynb file still produces a DENY (no regression)."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: single.ipynb\n@@ -1 +1 @@\n- old\n+ new\n",
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
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert "single.ipynb" in reason
        # No separator when there's only one reason
        assert "---" not in reason

    def test_error_on_one_file_still_denies_other(self, tmp_path, monkeypatch):
        """Exception on one file should not prevent denying based on another."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "good.py").write_text("# %%\nprint('good')\n", encoding="utf-8")
        (tmp_path / "bad.py").write_text("# %%\nprint('bad')\n", encoding="utf-8")

        call_count = [0]

        def fake_pre_drift_check(path, logger=None):
            call_count[0] += 1
            if "good" in str(path):
                return "Drift detected for good.py"
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "jupyter_jcli.commands.hooks_cmd._run_pre_drift_check",
            fake_pre_drift_check,
        )

        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    (
                        "*** Begin Patch\n"
                        "*** Update File: bad.py\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** Update File: good.py\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** End Patch"
                    ),
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
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "good.py" in output["hookSpecificOutput"]["permissionDecisionReason"]
        # bad.py should NOT appear since it raised an exception
        assert "bad.py" not in output["hookSpecificOutput"]["permissionDecisionReason"]
        assert call_count[0] == 2


class TestCodexMultiFilePostMerge:
    """Multi-file apply_patch post-edit should merge multiple context messages."""

    def test_multiple_contexts_merged(self, tmp_path, monkeypatch):
        """Multiple files producing post context → single merged PostToolUseContext."""
        monkeypatch.chdir(tmp_path)

        # Create .py files so path.exists() passes
        (tmp_path / "a.py").write_text("# %%\nprint('a')\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("# %%\nprint('b')\n", encoding="utf-8")

        call_count = [0]

        def fake_post_drift_check(path, logger=None):
            call_count[0] += 1
            # Return different messages per file
            if "a" in str(path):
                return "Auto-synced your edit in `a.py` to `a.ipynb`. Pair is now in sync."
            if "b" in str(path):
                return "Auto-synced your edit in `b.py` to `b.ipynb`. Pair is now in sync."
            return None

        monkeypatch.setattr(
            "jupyter_jcli.commands.hooks_cmd._run_post_drift_check",
            fake_post_drift_check,
        )

        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    (
                        "*** Begin Patch\n"
                        "*** Update File: a.py\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** Update File: b.py\n@@ -1 +1 @@\n- old\n+ new\n"
                        "*** End Patch"
                    ),
                ]
            },
            "hook_event_name": "PostToolUse",
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
            ["_hooks", "pair-drift-guard-post", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "a.py" in context
        assert "b.py" in context
        assert "---" in context
        assert call_count[0] == 2

    def test_single_context_no_separator(self, tmp_path, monkeypatch):
        """Single context message has no merge separator."""
        monkeypatch.chdir(tmp_path)

        # Create .py file so path.exists() passes
        (tmp_path / "only.py").write_text("# %%\nprint('only')\n", encoding="utf-8")

        def fake_post_drift_check(path, logger=None):
            return "Auto-synced your edit in `only.py` to `only.ipynb`. Pair is now in sync."

        monkeypatch.setattr(
            "jupyter_jcli.commands.hooks_cmd._run_post_drift_check",
            fake_post_drift_check,
        )

        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: only.py\n@@ -1 +1 @@\n- old\n+ new\n",
                ]
            },
            "hook_event_name": "PostToolUse",
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
            ["_hooks", "pair-drift-guard-post", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "only.py" in context
        assert "---" not in context

    def test_no_context_when_nothing_to_report(self, tmp_path, monkeypatch):
        """When no files produce context, stdout is empty."""
        monkeypatch.chdir(tmp_path)

        # Create .py file so path.exists() passes
        (tmp_path / "noop.py").write_text("# %%\nprint('noop')\n", encoding="utf-8")

        def fake_post_drift_check(path, logger=None):
            return None

        monkeypatch.setattr(
            "jupyter_jcli.commands.hooks_cmd._run_post_drift_check",
            fake_post_drift_check,
        )

        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "apply_patch",
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: noop.py\n@@ -1 +1 @@\n- old\n+ new\n",
                ]
            },
            "hook_event_name": "PostToolUse",
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
            ["_hooks", "pair-drift-guard-post", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == ""


