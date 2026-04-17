"""Tests for `j-cli _hooks notebook-exec-guard`."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _invoke(command: str) -> tuple[int, dict | None]:
    """Invoke notebook-exec-guard with a Bash command payload. Returns (exit_code, json_output)."""
    runner = CliRunner()
    payload = json.dumps({"tool_input": {"command": command}})
    result = runner.invoke(main, ["_hooks", "notebook-exec-guard"], input=payload, catch_exceptions=False)
    if result.output.strip():
        return result.exit_code, json.loads(result.output)
    return result.exit_code, None


def _is_deny(out: dict | None) -> bool:
    if out is None:
        return False
    return (
        out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


# ---------------------------------------------------------------------------
# Table-driven tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command, should_deny", [
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
])
def test_guard_decisions(command: str, should_deny: bool):
    exit_code, out = _invoke(command)
    assert exit_code == 0
    assert _is_deny(out) == should_deny, (
        f"command={command!r}: expected deny={should_deny}, got deny={_is_deny(out)}, output={out}"
    )


# ---------------------------------------------------------------------------
# Fail-open on bad input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_input", [
    "not json at all",
    "",
    "null",
    '{"tool_input": null}',
    '{"tool_input": {"command": null}}',
])
def test_malformed_stdin_allows(raw_input: str):
    runner = CliRunner()
    result = runner.invoke(
        main, ["_hooks", "notebook-exec-guard"], input=raw_input, catch_exceptions=False
    )
    assert result.exit_code == 0
    assert result.output.strip() == "", f"Expected empty stdout for input {raw_input!r}"


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


# ---------------------------------------------------------------------------
# Verify all four guard categories are active (CLI-level smoke test)
# ---------------------------------------------------------------------------

def test_all_guard_categories_active():
    """Each of the four intercepted tool families must produce a deny."""
    assert _is_deny(_invoke("papermill in.ipynb out.ipynb")[1])
    assert _is_deny(_invoke("runipy foo.ipynb")[1])
    assert _is_deny(_invoke("ipython foo.ipynb")[1])
    # nbconvert --execute via jupyter subcommand
    assert _is_deny(_invoke("jupyter nbconvert --execute foo.ipynb")[1])


# ---------------------------------------------------------------------------
# --debug smoke test for notebook-exec-guard
# ---------------------------------------------------------------------------

class TestNotebookExecGuardDebug:
    def test_debug_creates_log_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_input": {"command": "jupyter nbconvert --execute foo.ipynb"}})
        runner.invoke(main, ["_hooks", "notebook-exec-guard", "--debug"],
                      input=payload, catch_exceptions=False)
        logs = sorted(tmp_path.glob("notebook-exec-guard-*.log"))
        assert len(logs) == 1
        data = json.loads(logs[0].read_text())
        assert data["hook"] == "notebook-exec-guard"
        assert data["stdin_parsed"]["tool_input"]["command"] == "jupyter nbconvert --execute foo.ipynb"

    def test_debug_allow_path_logs_empty_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_input": {"command": "python foo.py"}})
        runner.invoke(main, ["_hooks", "notebook-exec-guard", "--debug"],
                      input=payload, catch_exceptions=False)
        data = json.loads(sorted(tmp_path.glob("notebook-exec-guard-*.log"))[0].read_text())
        assert data["stdout_raw"] == ""
        assert data["exit_code"] == 0
