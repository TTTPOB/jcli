"""Tests for `j-cli _hooks pair-drift-guard-pre`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.drift import DriftResult
from jupyter_jcli import pair_baseline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(payload: dict) -> tuple[int, dict | None]:
    """Invoke pair-drift-guard-pre with the given payload. Returns (exit_code, json_out).

    Parses the first valid JSON object from output; non-JSON lines (stderr notices
    mixed in by CliRunner) are skipped.
    """
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "pair-drift-guard-pre"],
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


def _additional_context(out: dict | None) -> str:
    if out is None:
        return ""
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def _make_pair(tmp_path: Path, py_src: list[str], ipynb_src: list[str]) -> tuple[Path, Path]:
    py = tmp_path / "nb.py"
    ipynb = tmp_path / "nb.ipynb"

    lines = ["# ---\n", "# jupyter:\n", "#   kernelspec:\n", "#     name: python3\n", "# ---\n\n"]
    for src in py_src:
        lines.append(f"# %%\n{src}\n\n")
    py.write_text("".join(lines), encoding="utf-8")

    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    for src in ipynb_src:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    ipynb.write_text(nbformat.writes(nb), encoding="utf-8")

    return py, ipynb


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _git_env(ts: int) -> dict[str, str]:
    env = os.environ.copy()
    stamp = f"@{ts} +0000"
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    return env


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# pair-drift-guard-pre no longer handles NotebookEdit (moved to notebook-edit-guard)
# ---------------------------------------------------------------------------

class TestNotebookEditPassThrough:
    def test_notebook_edit_is_allowed_by_pair_drift_guard_pre(self, tmp_path):
        """pair-drift-guard-pre no longer intercepts NotebookEdit — that's notebook-edit-guard's job."""
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(tmp_path / "nb.ipynb")},
        }
        code, out = _invoke(payload)
        assert code == 0
        # pair-drift-guard-pre should not emit a decision for NotebookEdit
        assert _decision(out) is None


# ---------------------------------------------------------------------------
# Direct Edit/Write of .ipynb -> always deny (pair-drift-guard-pre)
# ---------------------------------------------------------------------------

class TestDirectIpynbEditBlocked:
    def test_edit_existing_ipynb_is_denied(self, tmp_path):
        ipynb = tmp_path / "nb.ipynb"
        ipynb.write_text("{}", encoding="utf-8")
        code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})
        assert code == 0
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "nb.ipynb" in reason
        assert "py:percent" in reason or "round-trip" in reason

    def test_write_new_ipynb_is_denied(self, tmp_path):
        """Blocking creation of new .ipynb via Write is also covered."""
        ipynb = tmp_path / "new.ipynb"
        # file does not exist yet — Write would create it
        code, out = _invoke({"tool_name": "Write", "tool_input": {"file_path": str(ipynb)}})
        assert code == 0
        assert _decision(out) == "deny"

    def test_message_contains_round_trip_steps(self, tmp_path):
        ipynb = tmp_path / "nb.ipynb"
        ipynb.write_text("{}", encoding="utf-8")
        _, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})
        reason = _reason(out)
        assert "ipynb-to-py" in reason
        assert "py-to-ipynb" in reason
        assert "nb.py" in reason  # derived stem

    def test_py_file_edit_is_not_blocked(self, tmp_path):
        """Sanity: .py files still go through drift check, not this block."""
        py = tmp_path / "nb.py"
        py.write_text("x = 1\n", encoding="utf-8")
        code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        # no deny from the ipynb-block path (may still be allow from drift check)
        if _decision(out) is not None:
            assert _decision(out) != "deny" or "ipynb" not in _reason(out).lower()[:50]


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
        # New message: "Someone else edited ... you did not cause it"
        assert "Someone else edited" in reason or "Re-read" in reason or "nb.py" in reason

    def test_direct_ipynb_edit_is_always_denied(self, tmp_path):
        """Agent tries to Edit .ipynb directly — blocked regardless of drift state."""
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 99"])

        from tests.test_drift import _make_py_text, _make_ipynb_text

        base_py = _make_py_text("x = 1")

        with patch("jupyter_jcli.drift._get_git_base_text",
                   side_effect=lambda p: base_py if p.suffix == ".py" else None):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})

        assert code == 0
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "nb.ipynb" in reason
        assert "round-trip" in reason or "py:percent" in reason

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
        reason = _reason(out)
        assert "0" in reason  # cell index 0
        # New message: "Pre-existing conflict" and "This drift existed before"
        assert "Pre-existing conflict" in reason or "pre-existing" in reason.lower()
        assert "git diff" in reason

    def test_drift_only_count_mismatch_returns_deny(self, tmp_path):
        """No git base + cell count mismatch -> deny."""
        py, ipynb = _make_pair(tmp_path, ["x = 1", "y = 2"], ["x = 99"])
        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "not yet committed" in reason
        assert "git log" in reason

    def test_drift_only_content_diff_returns_deny(self, tmp_path):
        """No git base + different sources -> deny (DRIFT_ONLY, pick a side)."""
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 99"])
        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "not yet committed" in reason or "baseline" in reason.lower()


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
            main, ["_hooks", "pair-drift-guard-pre"], input=raw_input, catch_exceptions=False
        )
        assert result.exit_code == 0
        # No JSON decision emitted — only plain text notices allowed
        for line in result.output.splitlines():
            if line.strip().startswith("{"):
                assert False, f"Unexpected JSON in output for input {raw_input!r}: {line}"

    def test_drift_exception_allows(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])
        with patch("jupyter_jcli.commands.hooks_cmd._run_pre_drift_check", side_effect=RuntimeError("boom")):
            code, out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) is None  # allow (fail-open)


# ---------------------------------------------------------------------------
# notebook-edit-guard — always deny NotebookEdit
# ---------------------------------------------------------------------------

def _invoke_nb_edit_guard(payload: dict) -> tuple[int, dict | None]:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "notebook-edit-guard"],
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


class TestNotebookEditGuard:
    def test_notebook_edit_is_denied(self, tmp_path):
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(tmp_path / "nb.ipynb")},
        }
        code, out = _invoke_nb_edit_guard(payload)
        assert code == 0
        assert _decision(out) == "deny"
        reason = _reason(out)
        assert "NotebookEdit" in reason
        assert "py:percent" in reason or "round-trip" in reason

    def test_notebook_edit_denied_regardless_of_file(self):
        payload = {"tool_name": "NotebookEdit", "tool_input": {}}
        code, out = _invoke_nb_edit_guard(payload)
        assert code == 0
        assert _decision(out) == "deny"

    def test_edit_tool_is_allowed(self, tmp_path):
        """notebook-edit-guard only fires for NotebookEdit, not Edit."""
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "nb.py")}}
        code, out = _invoke_nb_edit_guard(payload)
        assert code == 0
        assert _decision(out) is None  # allow

    def test_malformed_stdin_allows(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["_hooks", "notebook-edit-guard"], input="not json", catch_exceptions=False
        )
        assert result.exit_code == 0
        for line in result.output.splitlines():
            if line.strip().startswith("{"):
                assert False, f"Unexpected JSON output on bad input: {line}"

    def test_message_contains_three_step_workflow(self):
        payload = {"tool_name": "NotebookEdit", "tool_input": {}}
        code, out = _invoke_nb_edit_guard(payload)
        reason = _reason(out)
        assert "ipynb-to-py" in reason
        assert "py-to-ipynb" in reason


# ---------------------------------------------------------------------------
# pair-drift-guard-post — PostToolUse auto-sync
# ---------------------------------------------------------------------------

def _invoke_post(payload: dict) -> tuple[int, dict | None]:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["_hooks", "pair-drift-guard-post"],
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


def _event_name(out: dict | None) -> str | None:
    if out is None:
        return None
    return out.get("hookSpecificOutput", {}).get("hookEventName")


class TestPairDriftGuardPost:
    """Tests for pair-drift-guard-post (PostToolUse)."""

    def test_in_sync_after_edit_is_silent(self, tmp_path):
        """Agent edits py, pair stays in sync -> no output."""
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])

        from tests.test_drift import _make_py_text
        base_py = _make_py_text("x = 1")

        with patch("jupyter_jcli.drift._get_git_base_text",
                   side_effect=lambda p: base_py if p.suffix == ".py" else None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None  # silent

    def test_auto_syncs_ipynb_after_py_edit(self, tmp_path):
        """Agent edits py (x=1->x=10), ipynb still has x=1 -> auto-sync ipynb."""
        from tests.test_drift import _make_ipynb_text, _make_py_text

        base_py = _make_py_text("x = 1")
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 1"])

        with patch("jupyter_jcli.drift._get_git_base_text",
                   side_effect=lambda p: base_py if p.suffix == ".py" else None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None
        ctx = _additional_context(out)
        assert "Auto-synced" in ctx
        assert "nb.py" in ctx
        assert "Pair is now in sync" in ctx
        assert _event_name(out) == "PostToolUse"

        # Verify ipynb was actually updated
        import nbformat as nbf
        nb = nbf.read(str(ipynb), as_version=4)
        non_empty = [c.source for c in nb.cells if c.source.strip()]
        assert non_empty == ["x = 10"]

    def test_ipynb_as_edited_file_in_post_is_silent(self, tmp_path):
        """Post hook silently exits for .ipynb — Pre should have blocked it already."""
        py, ipynb = _make_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 99"])

        from tests.test_drift import _make_py_text
        base_py = _make_py_text("x = 1", "y = 2")

        with patch("jupyter_jcli.drift._get_git_base_text",
                   side_effect=lambda p: base_py if p.suffix == ".py" else None):
            code, out = _invoke_post({"tool_name": "Write", "tool_input": {"file_path": str(ipynb)}})

        assert code == 0
        assert _decision(out) is None  # silent — Pre is the line of defense for ipynb

    def test_conflict_after_edit_warns(self, tmp_path):
        """Agent's edit creates a conflict -> warn with cell indices."""
        from tests.test_drift import _make_ipynb_text, _make_py_text

        base_py = _make_py_text("x = 1")
        # py has x=10, ipynb has x=99 -> both changed same cell -> conflict
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 99"])

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else None

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None
        ctx = _additional_context(out)
        assert "0" in ctx  # cell index
        assert "j-cli convert" in ctx
        assert _event_name(out) == "PostToolUse"

    def test_drift_only_count_mismatch_after_edit_warns(self, tmp_path):
        """py has no git baseline + count mismatch after agent's edit -> warn with convert hint."""
        py, ipynb = _make_pair(tmp_path, ["x = 10", "y = 20"], ["x = 99"])

        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None
        ctx = _additional_context(out)
        assert "j-cli convert" in ctx
        assert _event_name(out) == "PostToolUse"

    def test_drift_only_content_diff_after_edit_warns(self, tmp_path):
        """py has no git baseline + different sources -> context notification (DRIFT_ONLY, pick a side)."""
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 99"])

        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert _decision(out) is None
        ctx = _additional_context(out)
        assert "j-cli convert" in ctx
        assert _event_name(out) == "PostToolUse"

    def test_non_paired_file_is_silent(self, tmp_path):
        """Files without a paired counterpart are silently ignored."""
        solo = tmp_path / "script.py"
        solo.write_text("x = 1\n", encoding="utf-8")
        code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(solo)}})
        assert code == 0
        assert _decision(out) is None

    def test_malformed_stdin_allows(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["_hooks", "pair-drift-guard-post"], input="not json", catch_exceptions=False
        )
        assert result.exit_code == 0
        for line in result.output.splitlines():
            if line.strip().startswith("{"):
                assert False, f"Unexpected JSON output on bad input: {line}"

    def test_post_exception_allows(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])
        with patch("jupyter_jcli.commands.hooks_cmd._run_post_drift_check", side_effect=RuntimeError("boom")):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code == 0
        assert _decision(out) is None

    def test_ipynb_edit_in_post_is_silent(self, tmp_path):
        """If .ipynb somehow reached Post (Pre should have blocked it), exit silently."""
        py, ipynb = _make_pair(tmp_path, ["x = 1"], ["x = 1"])
        code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(ipynb)}})
        assert code == 0
        assert _decision(out) is None  # no output — Pre was the line of defense


# ---------------------------------------------------------------------------
# pair baseline integration — real git repo, no drift mock
# ---------------------------------------------------------------------------

class TestConsecutiveEdits:
    def test_post_uses_sticky_ref_to_avoid_false_conflict(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        py, ipynb = _make_pair(git_repo, ["x = 1"], ["x = 1"])
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "init", env=_git_env(100))

        py.write_text(py.read_text(encoding="utf-8").replace("x = 1", "x = 10"), encoding="utf-8")
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        code1, out1 = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code1 == 0
        assert "Auto-synced" in _additional_context(out1)
        first_ref = _git(
            git_repo,
            "log",
            "-1",
            "--format=%ct",
            pair_baseline._ref_name("nb.py"),
        ).stdout.strip()
        nb1 = nbformat.read(str(ipynb), as_version=4)
        assert [cell.source for cell in nb1.cells if cell.source.strip()] == ["x = 10"]

        py.write_text(py.read_text(encoding="utf-8").replace("x = 10", "x = 20"), encoding="utf-8")
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@160 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@160 +0000")
        code2, out2 = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code2 == 0
        assert "Auto-synced" in _additional_context(out2)
        second_ref = _git(
            git_repo,
            "log",
            "-1",
            "--format=%ct",
            pair_baseline._ref_name("nb.py"),
        ).stdout.strip()
        nb2 = nbformat.read(str(ipynb), as_version=4)
        assert [cell.source for cell in nb2.cells if cell.source.strip()] == ["x = 20"]
        assert int(second_ref) >= int(first_ref)


class TestPreToPostChain:
    def test_pre_merge_updates_ref_so_following_post_does_not_conflict(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        py, ipynb = _make_pair(git_repo, ["x = 1"], ["x = 1"])
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "init", env=_git_env(100))

        py.write_text(py.read_text(encoding="utf-8").replace("x = 1", "x = 10"), encoding="utf-8")
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        code0, out0 = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert code0 == 0
        assert "Auto-synced" in _additional_context(out0)

        nb = nbformat.read(str(ipynb), as_version=4)
        nb.cells[0].source = "x = 30"
        nbformat.write(nb, str(ipynb))

        monkeypatch.setenv("GIT_AUTHOR_DATE", "@170 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@170 +0000")
        pre_code, pre_out = _invoke({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})
        assert pre_code == 0
        assert _decision(pre_out) == "deny"
        assert "Re-read" in _reason(pre_out) or "Someone else edited" in _reason(pre_out)
        assert "x = 30" in py.read_text(encoding="utf-8")

        py.write_text(py.read_text(encoding="utf-8").replace("x = 30", "x = 40"), encoding="utf-8")
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@180 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@180 +0000")
        post_code, post_out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert post_code == 0
        assert "Auto-synced" in _additional_context(post_out)
        nb_after = nbformat.read(str(ipynb), as_version=4)
        assert [cell.source for cell in nb_after.cells if cell.source.strip()] == ["x = 40"]


class TestConvertSeededBaseline:
    def test_convert_then_first_edit_uses_sticky_baseline(self, git_repo: Path):
        py, ipynb = _make_pair(git_repo, ["x = 1"], ["x = 1"])
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "init", env=_git_env(100))

        py.write_text(py.read_text(encoding="utf-8").replace("x = 1", "x = 10"), encoding="utf-8")

        runner = CliRunner()
        convert = runner.invoke(
            main,
            ["convert", "py-to-ipynb", str(py), str(ipynb)],
            catch_exceptions=False,
        )
        assert convert.exit_code == 0

        ref_ts = _git(
            git_repo,
            "log",
            "-1",
            "--format=%ct",
            pair_baseline._ref_name("nb.py"),
        ).stdout.strip()
        assert ref_ts

        py.write_text(py.read_text(encoding="utf-8").replace("x = 10", "x = 20"), encoding="utf-8")
        code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert "Auto-synced" in _additional_context(out)
        nb_after = nbformat.read(str(ipynb), as_version=4)
        assert [cell.source for cell in nb_after.cells if cell.source.strip()] == ["x = 20"]


class TestGcPairSyncRefsCLI:
    def test_dry_run_reports_without_deleting(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        py, _ipynb = _make_pair(git_repo, ["x = 1"], ["x = 1"])
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "init", env=_git_env(100))

        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py, py.read_text(encoding="utf-8")) is True
        py.write_text(py.read_text(encoding="utf-8").replace("x = 1", "x = 2"), encoding="utf-8")
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "new head", env=_git_env(200))

        runner = CliRunner()
        monkeypatch.chdir(git_repo)
        result = runner.invoke(main, ["_hooks", "gc-pair-sync-refs", "--dry-run"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "would-remove" in (result.stderr or result.output)
        assert "removed 1, kept 0" in (result.stderr or result.output)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert refs.stdout.strip() != ""

    def test_cli_removes_orphan_ref(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        _git(git_repo, "commit", "--allow-empty", "-m", "init", env=_git_env(100))
        ghost_path = git_repo / "ghost.py"
        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(ghost_path, "# %%\nx = 1\n") is True

        runner = CliRunner()
        monkeypatch.chdir(git_repo)
        result = runner.invoke(main, ["_hooks", "gc-pair-sync-refs"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "remove" in (result.stderr or result.output)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert refs.stdout.strip() == ""

    def test_cli_removes_stale_head_older_ref(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        py, _ipynb = _make_pair(git_repo, ["x = 1"], ["x = 1"])
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "init", env=_git_env(100))

        monkeypatch.setenv("GIT_AUTHOR_DATE", "@150 +0000")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "@150 +0000")
        assert pair_baseline.write_baseline(py, py.read_text(encoding="utf-8")) is True
        py.write_text(py.read_text(encoding="utf-8").replace("x = 1", "x = 5"), encoding="utf-8")
        _git(git_repo, "add", "nb.py")
        _git(git_repo, "commit", "-m", "head newer", env=_git_env(200))

        runner = CliRunner()
        monkeypatch.chdir(git_repo)
        result = runner.invoke(main, ["_hooks", "gc-pair-sync-refs"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "removed 1, kept 0" in (result.stderr or result.output)
        refs = _git(git_repo, "for-each-ref", "refs/jcli/pair-sync/", "--format=%(refname)")
        assert refs.stdout.strip() == ""


# ---------------------------------------------------------------------------
# --debug smoke tests for pair-drift-guard-pre and pair-drift-guard-post
# ---------------------------------------------------------------------------

class TestPairDriftGuardPreDebug:
    def test_debug_creates_log_for_pre(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "nonexistent.py"}})
        runner.invoke(main, ["_hooks", "pair-drift-guard-pre", "--debug"],
                      input=payload, catch_exceptions=False)
        logs = sorted(tmp_path.glob("pair-drift-guard-pre-*.log"))
        assert len(logs) == 1
        data = json.loads(logs[0].read_text())
        assert data["hook"] == "pair-drift-guard-pre"
        assert data["exit_code"] == 0
        assert data["stdout_raw"] == ""

    def test_debug_creates_log_for_post(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "nonexistent.py"}})
        runner.invoke(main, ["_hooks", "pair-drift-guard-post", "--debug"],
                      input=payload, catch_exceptions=False)
        logs = sorted(tmp_path.glob("pair-drift-guard-post-*.log"))
        assert len(logs) == 1
        data = json.loads(logs[0].read_text())
        assert data["hook"] == "pair-drift-guard-post"
        assert data["exit_code"] == 0

    def test_debug_notebook_edit_guard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        runner = CliRunner()
        payload = json.dumps({"tool_name": "NotebookEdit", "tool_input": {}})
        runner.invoke(main, ["_hooks", "notebook-edit-guard", "--debug"],
                      input=payload, catch_exceptions=False)
        logs = sorted(tmp_path.glob("notebook-edit-guard-*.log"))
        assert len(logs) == 1
        data = json.loads(logs[0].read_text())
        assert data["hook"] == "notebook-edit-guard"
        assert data["stdout_parsed"]["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# PostToolUse wire schema — assert additionalContext, no permissionDecision
# ---------------------------------------------------------------------------

class TestPostToolUseSchema:
    """PostToolUse hook must emit additionalContext, never permissionDecision."""

    def test_conflict_post_schema(self, tmp_path):
        from tests.test_drift import _make_py_text
        base_py = _make_py_text("x = 1")
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 99"])

        def _git_side(path: Path) -> str | None:
            return base_py if path.suffix == ".py" else None

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_git_side):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "additionalContext" in hso
        assert "drift detected" in hso["additionalContext"]
        assert "j-cli convert" in hso["additionalContext"]
        assert "permissionDecision" not in hso
        assert "decision" not in out

    def test_drift_only_post_schema(self, tmp_path):
        py, ipynb = _make_pair(tmp_path, ["x = 10", "y = 20"], ["x = 99"])

        with patch("jupyter_jcli.drift._get_git_base_text", return_value=None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "additionalContext" in hso
        assert "drift detected" in hso["additionalContext"]
        assert "j-cli convert" in hso["additionalContext"]
        assert "permissionDecision" not in hso
        assert "decision" not in out

    def test_auto_synced_post_schema(self, tmp_path):
        from tests.test_drift import _make_py_text
        base_py = _make_py_text("x = 1")
        py, ipynb = _make_pair(tmp_path, ["x = 10"], ["x = 1"])

        with patch("jupyter_jcli.drift._get_git_base_text",
                   side_effect=lambda p: base_py if p.suffix == ".py" else None):
            code, out = _invoke_post({"tool_name": "Edit", "tool_input": {"file_path": str(py)}})

        assert code == 0
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "additionalContext" in hso
        assert "Auto-synced" in hso["additionalContext"]
        assert "permissionDecision" not in hso
        assert "decision" not in out
