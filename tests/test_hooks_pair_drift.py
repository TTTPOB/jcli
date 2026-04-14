"""Tests for `j-cli _hooks pair-drift-guard`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.drift import DriftResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(payload: dict) -> tuple[int, dict | None]:
    """Invoke pair-drift-guard with the given payload. Returns (exit_code, json_out).

    Parses the first valid JSON object from output; non-JSON lines (stderr notices
    mixed in by CliRunner) are skipped.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "pair-drift-guard"],
        input=json.dumps(payload),
        catch_exceptions=False,
    )
    for line in result.output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return result.exit_code, json.loads(line)
            except json.JSONDecodeError:
                continue
    return result.exit_code, None


def _decision(out: dict | None) -> str | None:
    if out is None:
        return None
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def _reason(out: dict | None) -> str:
    if out is None:
        return ""
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def _make_pair(tmp_path: Path, py_src: list[str], ipynb_src: list[str]) -> tuple[Path, Path]:
    py = tmp_path / "nb.py"
    ipynb = tmp_path / "nb.ipynb"

    lines = ["# ---\n", "# jupyter:\n", "#   kernelspec:\n", "#     name: python3\n", "# ---\n\n"]
    for src in py_src:
        lines.append(f"# %%\n{src}\n\n")
    py.write_text("".join(lines), encoding="utf-8")

    nb = nbformat.v4.new_notebook()
    for src in ipynb_src:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    ipynb.write_text(nbformat.writes(nb), encoding="utf-8")

    return py, ipynb


# ---------------------------------------------------------------------------
# NotebookEdit -> always deny
# ---------------------------------------------------------------------------

class TestNotebookEditDenied:
    def test_notebook_edit_is_always_denied(self, tmp_path):
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(tmp_path / "nb.ipynb")},
        }
        code, out = _invoke(payload)
        assert code == 0
        assert _decision(out) == "deny"
        assert "NotebookEdit" in _reason(out) or "py:percent" in _reason(out)

    def test_notebook_edit_denied_regardless_of_file(self):
        payload = {"tool_name": "NotebookEdit", "tool_input": {}}
        code, out = _invoke(payload)
        assert code == 0
        assert _decision(out) == "deny"


# ---------------------------------------------------------------------------
# Non-paired files -> allow
# ---------------------------------------------------------------------------

class TestNonPairedFiles:
    def test_py_without_pair_allows(self, tmp_path):
        py = tmp_path / "solo.py"
        py.write_text("x = 1\n", encoding="utf-8")
        code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) is None  # allow (empty stdout)

    def test_nonexistent_file_allows(self, tmp_path):
        code, out = _invoke({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "ghost.py")},
        })
        assert code == 0
        assert _decision(out) is None


# ---------------------------------------------------------------------------
# Paired files — drift-free -> allow
# ---------------------------------------------------------------------------

class TestNoDrift:
    def test_in_sync_pair_allows(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])
        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) is None


# ---------------------------------------------------------------------------
# Auto-merge: only other side changed -> allow, file written
# ---------------------------------------------------------------------------

class TestAutoMergeOtherSide:
    def test_ipynb_changed_py_is_target_deny(self, tmp_path):
        """ipynb drifted (x=1->x=99), agent edits py (still has x=1).
        Merged = x=99. py needs update. py IS target -> deny (agent's old_string stale).
        """
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 99"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else base_ipynb

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        # merged=x=99; py needs update (x=1->x=99); py IS target -> deny
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "nb.py" in reason or "Re-read" in reason or "re-read" in reason.lower()

    def test_ipynb_changed_agent_edits_ipynb_allows(self, tmp_path):
        """ipynb drifted (x=1->x=99), agent edits ipynb.
        Merged = x=99. ipynb already has x=99 -> no ipynb update.
        py needs update (x=1->x=99). py is OTHER side -> allow, py written.
        """
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 99"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else base_ipynb

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})

        assert code == 0
        assert _decision(out) is None  # allow — ipynb unchanged, py (other side) was synced

    def test_py_changed_ipynb_is_target_deny(self, tmp_path):
        """py drifted (x=1->x=99), agent edits ipynb (still has x=1).
        Merged = x=99. ipynb needs update. ipynb IS target -> deny.
        """
        py, ipynb = _make_pair(tmp_path, ["x = 99"], ["x = 1"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else base_ipynb

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})

        assert code == 0
        # merged=x=99; ipynb needs update (x=1->x=99); ipynb IS target -> deny
        assert _decision(out) == "deny"

    def test_py_changed_agent_edits_py_allows(self, tmp_path):
        """py drifted (x=1->x=99), agent edits py.
        Merged = x=99. py already has x=99 -> no py update.
        ipynb needs update (x=1->x=99). ipynb is OTHER side -> allow.
        """
        py, ipynb = _make_pair(tmp_path, ["x = 99"], ["x = 1"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else base_ipynb

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None  # allow — py unchanged, ipynb (other side) was synced


# (TestAutoMergeTargetDeny scenarios are now covered in TestAutoMergeOtherSide above)


# ---------------------------------------------------------------------------
# Conflict -> deny
# ---------------------------------------------------------------------------

class TestConflict:
    def test_conflict_returns_deny(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 99"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else base_ipynb

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) == "deny"
        assert "0" in _reason(out)  # cell index 0 in reason

    def test_drift_only_returns_deny(self, tmp_path):
        """No git base + unequal cells -> deny."""
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 99"])
        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) == "deny"


# ---------------------------------------------------------------------------
# Fail-open on bad input / exceptions
# ---------------------------------------------------------------------------

class TestFailOpen:
    @pytest.mark.parametrize("raw_input", [
        "not json",
        "",
        "null",
        '{"tool_name": null}',
    ])
    def test_malformed_stdin_allows(self, raw_input: str):
        runner = CliRunner()
        result = runner.invoke(
            main, ["_hooks", "pair-drift-guard"], input=raw_input, catch_exceptions=False
        )
        assert result.exit_code == 0
        # No JSON decision emitted — only plain text notices allowed
        for line in result.output.splitlines():
            if line.strip().startswith("{"):
                assert False, f"Unexpected JSON in output for input {raw_input!r}: {line}"

    def test_drift_exception_allows(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])
        with patch("jupyter_jcli.commands.hooks_cmd._run_drift_check", side_effect=RuntimeError("boom")):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) is None  # allow (fail-open)
