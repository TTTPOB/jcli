"""Tests for `j-cli _hooks python-run-guard`."""

import json

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(command: str, cwd: str) -> tuple[int, dict | None]:
    """Invoke python-run-guard with a Bash command payload. Returns (exit_code, json_output)."""
    runner = CliRunner()
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
    result = runner.invoke(
        main, ["_hooks", "python-run-guard"], input=payload, catch_exceptions=False
    )
    if result.output.strip():
        return result.exit_code, json.loads(result.output)
    return result.exit_code, None


def _is_deny(out: dict | None) -> bool:
    if out is None:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# Paired + intercept (deny) — foo.py + foo.ipynb staged in tmp_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "python foo.py",
    "python3 foo.py",
    "uv run python foo.py",
    "uv run -p 3.12 python foo.py",
    "pixi run python foo.py",
    "pixi run -e dev python foo.py",
    "./foo.py",
    "cd /tmp && python foo.py",
    # Regression: gaps fixed by tree-sitter parser
    "python -u foo.py",               # P1: -u flag before .py was missed by regex
    "FOO=bar python foo.py",          # env-var prefix
    "A=1 B=2 python foo.py",          # multiple env-var prefixes
    "conda run python foo.py",        # conda runner wrapper
    "poetry run python foo.py",       # poetry runner wrapper
    "env python foo.py",              # env wrapper
    "nohup python foo.py",            # nohup wrapper
    "env FOO=bar python foo.py",      # env with inline assignment
    "conda run -n myenv python foo.py",  # conda with -n flag
])
def test_paired_intercept(command: str, tmp_path):
    (tmp_path / "foo.py").touch()
    (tmp_path / "foo.ipynb").touch()
    exit_code, out = _invoke(command, str(tmp_path))
    assert exit_code == 0
    assert _is_deny(out), (
        f"command={command!r}: expected deny but got allow, output={out}"
    )


def test_dummy_py_paired_intercept(tmp_path):
    """bar.dummy.py + bar.ipynb should also be intercepted."""
    (tmp_path / "bar.dummy.py").touch()
    (tmp_path / "bar.ipynb").touch()
    exit_code, out = _invoke("python bar.dummy.py", str(tmp_path))
    assert exit_code == 0
    assert _is_deny(out)


# ---------------------------------------------------------------------------
# Unpaired + allow silently — tools.py (no tools.ipynb) in tmp_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "python tools.py",
    "uv run python tools.py",
    "./tools.py",
])
def test_unpaired_allow(command: str, tmp_path):
    (tmp_path / "tools.py").touch()
    exit_code, out = _invoke(command, str(tmp_path))
    assert exit_code == 0
    assert out is None, (
        f"command={command!r}: expected empty stdout (allow) but got {out}"
    )


# ---------------------------------------------------------------------------
# Stage-1 non-match + allow — even when paired files exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    'python -c "print(1)"',
    "python -m pytest",
    "pytest foo.py",
    "uv run pytest",
    "pixi run test",
    "./configure",
    "./build.sh",
    "echo 'python foo.py'",
    "ls -la",
    # Regression: quoted strings must not produce false positives
    'echo "python foo.py"',           # double-quoted arg is not a command
    "bash -c 'python foo.py'",        # single-quoted string inside bash -c
])
def test_non_match_allow(command: str, tmp_path):
    # Stage paired files so the guard *would* fire if the regex matched.
    (tmp_path / "foo.py").touch()
    (tmp_path / "foo.ipynb").touch()
    exit_code, out = _invoke(command, str(tmp_path))
    assert exit_code == 0
    assert out is None, (
        f"command={command!r}: expected empty stdout but got {out}"
    )


# ---------------------------------------------------------------------------
# Fail-open on malformed stdin
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
        main, ["_hooks", "python-run-guard"], input=raw_input, catch_exceptions=False
    )
    assert result.exit_code == 0
    assert result.output.strip() == "", (
        f"Expected empty stdout for input {raw_input!r}"
    )


# ---------------------------------------------------------------------------
# Decision shape test
# ---------------------------------------------------------------------------

def test_decision_shape(tmp_path):
    """On intercept, verify the full structure and content of the deny decision."""
    (tmp_path / "foo.py").touch()
    (tmp_path / "foo.ipynb").touch()

    exit_code, out = _invoke("python foo.py", str(tmp_path))
    assert exit_code == 0
    assert out is not None

    hook_out = out["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"

    reason: str = hook_out["permissionDecisionReason"]
    assert "foo.py" in reason
    assert "foo.ipynb" in reason
    assert "j-cli" in reason
    assert "session" in reason
    assert "kernel" in reason
    # Reconsider / think-carefully framing
    assert "Reconsider" in reason or "reconsider" in reason
    assert "Think carefully" in reason or "think carefully" in reason
