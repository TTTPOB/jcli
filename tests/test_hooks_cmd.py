"""Tests for `j-cli _hooks notebook-exec-guard`."""

import json

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "guard",
    [
        "notebook-exec-guard",
        "python-run-guard",
        "pair-drift-guard-pre",
        "pair-drift-guard-post",
        "notebook-edit-guard",
    ],
)
def test_unknown_platform_rejected_before_reading_payload(guard):
    result = CliRunner().invoke(
        main, ["_hooks", guard, "--platform", "typo"], input="not json"
    )
    assert result.exit_code == 2
    assert "Invalid value for '--platform'" in result.output
    assert "typo" in result.output


@pytest.mark.parametrize("platform", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("tool_name, expected_code", [("NotebookEdit", 2), ("Edit", 0)])
def test_notebook_edit_guard_uses_same_tool_name_check_for_all_formats(
    platform, tool_name, expected_code
):
    result = CliRunner().invoke(
        main,
        ["_hooks", "notebook-edit-guard", "--platform", platform],
        input=json.dumps({"tool_name": tool_name, "tool_input": {}}),
        catch_exceptions=False,
    )
    assert result.exit_code == expected_code
    if expected_code == 2:
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
    else:
        assert result.stdout == ""


def _invoke(command: str) -> tuple[int, dict | None]:
    """Invoke notebook-exec-guard and parse its optional JSON decision."""
    runner = CliRunner()
    payload = json.dumps({"tool_input": {"command": command}})
    result = runner.invoke(
        main, ["_hooks", "notebook-exec-guard"], input=payload, catch_exceptions=False
    )
    for line in result.output.splitlines():
        if line.strip().startswith("{"):
            return result.exit_code, json.loads(line)
    return result.exit_code, None


def _is_deny(out: dict | None) -> bool:
    if out is None:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# Table-driven tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, should_deny",
    [
        # nbconvert --execute variants — all should deny
        ("jupyter nbconvert --to notebook --execute foo.ipynb", True),
        ("jupyter nbconvert --execute foo.ipynb", True),
        ("python -m jupyter nbconvert --execute foo.ipynb", True),
        ("uv run jupyter nbconvert --execute foo.ipynb", True),
        ("cd /tmp && jupyter nbconvert --execute foo.ipynb", True),
        # nbconvert without --execute — must allow
        ("jupyter nbconvert --to html foo.ipynb", False),
        # papermill — deny
        ("papermill in.ipynb out.ipynb", True),
        ("uv run papermill in.ipynb out.ipynb", True),
        # runipy — deny
        ("runipy foo.ipynb", True),
        ("uv run runipy foo.ipynb", True),
        # ipython forms — deny
        ('ipython -c "%run foo.ipynb"', True),
        ("ipython foo.ipynb", True),
        # ipython without notebook — allow
        ('ipython -c "print(1)"', False),
        # safe commands — allow
        ("ls -la", False),
        ("echo hello", False),
        ("python script.py", False),
        # single-quoted string inside echo: correctly allowed (AST context)
        ("echo 'jupyter nbconvert --execute'", False),
        # Regression: double-quoted string must not cause false positive
        ('echo "jupyter nbconvert --execute foo.ipynb"', False),
        # Regression: G1 false positive — --execute in a later echo must not
        # bleed through DOTALL lookahead into the preceding nbconvert command
        ("ls x.ipynb; echo --execute", False),
        # Regression: new wrapper support
        ("conda run jupyter nbconvert --execute foo.ipynb", True),
        ("poetry run papermill in.ipynb out.ipynb", True),
        ("env runipy foo.ipynb", True),
    ],
)
def test_guard_decisions(command: str, should_deny: bool):
    exit_code, out = _invoke(command)
    assert exit_code == (2 if should_deny else 0)
    assert _is_deny(out) == should_deny, (
        f"command={command!r}: expected deny={should_deny}, got deny={_is_deny(out)}, output={out}"
    )


# ---------------------------------------------------------------------------
# Malformed hook input is an operation failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_input",
    [
        "not json at all",
        "",
        "null",
        '{"tool_input": null}',
        '{"tool_input": {"command": null}}',
    ],
)
def test_malformed_stdin_fails(raw_input: str):
    runner = CliRunner()
    result = runner.invoke(
        main, ["_hooks", "notebook-exec-guard"], input=raw_input, catch_exceptions=False
    )
    assert result.exit_code == 1
    assert "malformed hook payload" in (result.stderr or result.output)


def test_malformed_dsh_payload_is_nonzero():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "notebook-exec-guard", "--platform", "dsh"],
        input=json.dumps({"tool_input": None}),
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "malformed hook payload" in (result.stderr or result.output)


@pytest.mark.parametrize(
    ("hook", "payload"),
    [
        (
            "notebook-exec-guard",
            {"tool_input": {"command": 42}},
        ),
        (
            "notebook-exec-guard",
            {"tool_input": {"command": ["bash", "-c", "echo ok"]}},
        ),
        (
            "python-run-guard",
            {"cwd": 42, "tool_input": {"command": "echo ok"}},
        ),
        (
            "python-run-guard",
            {"tool_input": {"command": ["bash", "-c", "echo ok"]}},
        ),
        (
            "pair-drift-guard-pre",
            {"tool_input": {"file_path": None}},
        ),
        (
            "pair-drift-guard-post",
            {"tool_input": {"file_path": None}},
        ),
        (
            "notebook-edit-guard",
            {"tool_name": None, "tool_input": {}},
        ),
    ],
)
def test_explicit_wrong_payload_types_fail(hook: str, payload: dict):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", hook, "--platform", "dsh"],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "malformed hook payload" in (result.stderr or result.output)


@pytest.mark.parametrize("hook", ["notebook-exec-guard", "python-run-guard"])
def test_missing_dsh_command_is_normal_noop(hook: str):
    result = CliRunner().invoke(
        main,
        ["_hooks", hook, "--platform", "dsh"],
        input=json.dumps({"tool_input": {}}),
        catch_exceptions=False,
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Deny message quality
# ---------------------------------------------------------------------------


def test_deny_message_mentions_label():
    """The permissionDecisionReason should name the blocked tool."""
    _, out = _invoke("papermill in.ipynb out.ipynb")
    reason: str = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "papermill" in reason
    assert "j-cli" in reason


def test_deny_message_mentions_nbconvert_label():
    _, out = _invoke("jupyter nbconvert --execute foo.ipynb")
    reason: str = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "nbconvert" in reason


def test_deny_exit2_writes_reason_to_stderr():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "notebook-exec-guard"],
        input=json.dumps({"tool_input": {"command": "papermill in.ipynb out.ipynb"}}),
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "papermill" in (result.stderr or result.output)


# ---------------------------------------------------------------------------
# --debug smoke test for notebook-exec-guard
# ---------------------------------------------------------------------------


class TestNotebookExecGuardDebug:
    def test_debug_creates_log_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps(
            {"tool_input": {"command": "jupyter nbconvert --execute foo.ipynb"}}
        )
        result = runner.invoke(
            main,
            ["_hooks", "notebook-exec-guard", "--debug"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        logs = sorted(tmp_path.glob("notebook-exec-guard-*.log"))
        assert len(logs) == 1
        data = json.loads(logs[0].read_text())
        assert data["hook"] == "notebook-exec-guard"
        assert (
            data["stdin_parsed"]["tool_input"]["command"]
            == "jupyter nbconvert --execute foo.ipynb"
        )
        assert data["exit_code"] == 2

    def test_debug_allow_path_logs_empty_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_input": {"command": "python foo.py"}})
        runner.invoke(
            main,
            ["_hooks", "notebook-exec-guard", "--debug"],
            input=payload,
            catch_exceptions=False,
        )
        data = json.loads(min(tmp_path.glob("notebook-exec-guard-*.log")).read_text())
        assert data["stdout_raw"] == ""
        assert data["exit_code"] == 0
