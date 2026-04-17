"""Tests for jupyter_jcli.hook_debug — HookDebugLogger structure and edge cases."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jupyter_jcli.hook_debug import HookDebugLogger, read_hook_stdin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("*.log"))


# ---------------------------------------------------------------------------
# Basic log creation
# ---------------------------------------------------------------------------

class TestHookDebugLoggerBasic:
    def test_creates_log_file_on_exit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("test-hook", enabled=True) as log:
            log.set_stdin('{"a": 1}', {"a": 1})
        files = _log_files(tmp_path)
        assert len(files) == 1
        assert files[0].name.startswith("test-hook-")

    def test_log_json_structure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("my-hook", enabled=True) as log:
            log.set_stdin('{"x": 2}', {"x": 2})
            log.set_stdout('{"ok": true}', {"ok": True})
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["hook"] == "my-hook"
        assert data["stdin_raw"] == '{"x": 2}'
        assert data["stdin_parsed"] == {"x": 2}
        assert data["stdout_raw"] == '{"ok": true}'
        assert data["stdout_parsed"] == {"ok": True}
        assert data["stderr"] == ""
        assert data["exception"] is None
        assert "pid" in data
        assert "timestamp" in data
        assert "duration_ms" in data
        assert data["exit_code"] == 0

    def test_no_log_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("test-hook", enabled=False) as log:
            log.set_stdin('{"a": 1}', {"a": 1})
        assert _log_files(tmp_path) == []


# ---------------------------------------------------------------------------
# exit_code capture
# ---------------------------------------------------------------------------

class TestExitCodeCapture:
    def test_exit_code_zero_on_sys_exit_0(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with pytest.raises(SystemExit):
            with HookDebugLogger("hook", enabled=True):
                sys.exit(0)
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["exit_code"] == 0

    def test_exit_code_one_on_sys_exit_1(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with pytest.raises(SystemExit):
            with HookDebugLogger("hook", enabled=True):
                sys.exit(1)
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["exit_code"] == 1

    def test_exit_code_zero_on_clean_exit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("hook", enabled=True):
            pass
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Exception recording
# ---------------------------------------------------------------------------

class TestExceptionRecording:
    def test_record_exception_populates_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("hook", enabled=True) as log:
            try:
                raise ValueError("boom")
            except ValueError as exc:
                log.record_exception(exc)
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["exception"]["type"] == "ValueError"
        assert data["exception"]["message"] == "boom"
        assert "traceback" in data["exception"]
        assert "ValueError" in data["exception"]["traceback"]

    def test_unhandled_exception_recorded_and_reraised(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with pytest.raises(RuntimeError, match="unexpected"):
            with HookDebugLogger("hook", enabled=True):
                raise RuntimeError("unexpected")
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["exception"]["type"] == "RuntimeError"
        assert data["exit_code"] == 1


# ---------------------------------------------------------------------------
# Silent exit (stdout_raw == "")
# ---------------------------------------------------------------------------

class TestSilentExit:
    def test_silent_return_stdout_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        with HookDebugLogger("hook", enabled=True) as log:
            log.set_stdin('{"tool_name": "Read"}', {"tool_name": "Read"})
            # no set_stdout call — simulates IN_SYNC silent return
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["stdout_raw"] == ""
        assert data["stdout_parsed"] is None
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# JSONDecodeError: stdin_raw preserved, stdin_parsed is null
# ---------------------------------------------------------------------------

class TestJsonDecodeError:
    def test_bad_stdin_raw_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        bad_input = "not json {"
        with HookDebugLogger("hook", enabled=True) as log:
            log.set_stdin(bad_input, None)
            # parsed stays None, raw is preserved
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["stdin_raw"] == bad_input
        assert data["stdin_parsed"] is None


# ---------------------------------------------------------------------------
# stderr capture
# ---------------------------------------------------------------------------

class TestStderrCapture:
    def test_stderr_captured_in_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        import sys as _sys
        with HookDebugLogger("hook", enabled=True):
            print("some warning", file=_sys.stderr)
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert "some warning" in data["stderr"]


# ---------------------------------------------------------------------------
# read_hook_stdin helper
# ---------------------------------------------------------------------------

class TestReadHookStdin:
    def test_sets_raw_and_parsed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}
        raw = json.dumps(payload)
        import io
        with patch("sys.stdin", io.StringIO(raw)):
            with HookDebugLogger("hook", enabled=True) as log:
                result = read_hook_stdin(log)
        assert result == payload
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["stdin_raw"] == raw
        assert data["stdin_parsed"] == payload

    def test_raises_on_bad_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCLI_DEBUG_LOG_DIR", str(tmp_path))
        import io
        with patch("sys.stdin", io.StringIO("bad json")):
            with HookDebugLogger("hook", enabled=True) as log:
                with pytest.raises(json.JSONDecodeError):
                    read_hook_stdin(log)
        data = json.loads(_log_files(tmp_path)[0].read_text())
        assert data["stdin_raw"] == "bad json"
        assert data["stdin_parsed"] is None
