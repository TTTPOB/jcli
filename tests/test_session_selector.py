"""Tests for session ID display and selector resolution."""

import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.server import ServerClient
from jupyter_jcli.session_selector import (
    SessionSelectorAmbiguous,
    SessionSelectorError,
    SessionSelectorNotFound,
    resolve_session_selector,
    short_session_ids,
    with_session_selectors,
)
from jupyter_jcli.variables import VariableSource

SESSIONS = [
    {
        "session_id": "abc1-session-id",
        "kernel_id": "kernel-one",
        "kernel_name": "python3",
        "kernel_state": "idle",
        "name": "analysis",
    },
    {
        "session_id": "abc2-session-id",
        "kernel_id": "kernel-two",
        "kernel_name": "python3",
        "kernel_state": "idle",
        "name": "report",
    },
    {
        "session_id": "xyz3-session-id",
        "kernel_id": "kernel-three",
        "kernel_name": "python3",
        "kernel_state": "idle",
        "name": "sandbox",
    },
]


def _list_sessions(self):
    return SESSIONS


def test_short_session_ids_start_at_three_and_expand_for_collisions():
    assert short_session_ids(SESSIONS) == {
        "abc1-session-id": "abc1",
        "abc2-session-id": "abc2",
        "xyz3-session-id": "xyz",
    }


def test_with_session_selectors_preserves_full_ids_without_mutating_input():
    sessions = with_session_selectors(SESSIONS)

    assert [session["session_selector"] for session in sessions] == [
        "abc1",
        "abc2",
        "xyz",
    ]
    assert [session["session_id"] for session in sessions] == [
        "abc1-session-id",
        "abc2-session-id",
        "xyz3-session-id",
    ]
    assert all("session_selector" not in session for session in SESSIONS)


@pytest.mark.parametrize(
    "args", [["session", "list"], ["session", "list", "--no-vars"]]
)
def test_session_list_human_uses_short_ids_and_json_keeps_full_ids(monkeypatch, args):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    monkeypatch.setattr(
        "jupyter_jcli.commands.session._enrich_with_vars", lambda *args: None
    )
    runner = CliRunner()

    human = runner.invoke(main, args)
    assert human.exit_code == 0, human.output
    assert "abc1" in human.output
    assert "abc2" in human.output
    assert "xyz" in human.output
    assert "abc1-session-id" not in human.output
    assert "SESSION_ID" not in human.output

    json_result = runner.invoke(main, ["--json", "session", "list", "--no-vars"])
    assert json_result.exit_code == 0, json_result.output
    assert [
        session["session_id"] for session in json.loads(json_result.output)["sessions"]
    ] == [
        "abc1-session-id",
        "abc2-session-id",
        "xyz3-session-id",
    ]
    assert [
        session["session_selector"]
        for session in json.loads(json_result.output)["sessions"]
    ] == ["abc1", "abc2", "xyz"]


def test_resolve_session_selector_accepts_full_id_short_id_and_name():
    assert resolve_session_selector(SESSIONS, "abc1-session-id") == "abc1-session-id"
    assert resolve_session_selector(SESSIONS, "abc1") == "abc1-session-id"
    assert resolve_session_selector(SESSIONS, "analysis") == "abc1-session-id"


def test_server_client_resolve_session_uses_active_sessions(monkeypatch):
    calls = []
    server = Mock(spec=ServerClient)
    server.list_sessions.return_value = SESSIONS
    server.resolve_session = ServerClient.resolve_session.__get__(server)
    monkeypatch.setattr(
        "jupyter_jcli.server.resolve_session_selector",
        lambda sessions, selector: calls.append((sessions, selector)) or "session-one",
    )

    assert server.resolve_session("analysis") == "session-one"
    assert calls == [(SESSIONS, "analysis")]
    server.list_sessions.assert_called_once_with()


def test_server_client_resolve_kernel_reuses_client():
    server = Mock(spec=ServerClient)
    server.resolve_session.return_value = "session-one"
    server.get_kernel_id_for_session.return_value = "kernel-one"
    server.resolve_kernel = ServerClient.resolve_kernel.__get__(server)

    assert server.resolve_kernel("analysis") == ("session-one", "kernel-one")
    server.resolve_session.assert_called_once_with("analysis")
    server.get_kernel_id_for_session.assert_called_once_with("session-one")


@pytest.mark.parametrize(
    "sessions, selector",
    [
        (SESSIONS, "abc"),
        ([{**SESSIONS[0], "name": "same"}, {**SESSIONS[1], "name": "same"}], "same"),
        ([{**SESSIONS[0], "name": "xyz"}, SESSIONS[2]], "xyz"),
    ],
)
def test_resolve_session_selector_rejects_ambiguous_prefix_or_name(sessions, selector):
    with pytest.raises(
        SessionSelectorAmbiguous, match="matches multiple active sessions"
    ):
        resolve_session_selector(sessions, selector)


def test_resolve_session_selector_reports_no_match():
    with pytest.raises(SessionSelectorNotFound, match="No active session matches"):
        resolve_session_selector(SESSIONS, "missing")


@pytest.mark.parametrize(
    "error_type, code",
    [
        (SessionSelectorNotFound, "SESSION_NOT_FOUND"),
        (SessionSelectorAmbiguous, "SESSION_SELECTOR_AMBIGUOUS"),
    ],
)
def test_session_selector_errors_have_stable_codes(error_type, code):
    error = error_type("resolution failed")

    assert isinstance(error, SessionSelectorError)
    assert error.code == code


def test_command_reports_unmatched_selector(monkeypatch):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)

    result = CliRunner().invoke(main, ["--json", "vars", "missing"])

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error["code"] == "SESSION_NOT_FOUND"
    assert "No active session matches selector 'missing'" in error["message"]


@pytest.mark.parametrize(
    "args, resolver",
    [
        (["session", "kill", "session"], "resolve_session"),
        (["kernel", "interrupt", "session"], "resolve_kernel"),
        (["kernel", "restart", "session"], "resolve_kernel"),
        (["exec", "session", "--code", "pass"], "resolve_kernel"),
        (["vars", "session"], "resolve_kernel"),
    ],
)
@pytest.mark.parametrize(
    "error_type",
    [SessionSelectorNotFound, SessionSelectorAmbiguous],
)
def test_commands_render_session_selector_errors(
    monkeypatch, args, resolver, error_type
):
    def raise_selector_error(self, selector):
        raise error_type("resolution failed")

    monkeypatch.setattr(ServerClient, resolver, raise_selector_error)

    result = CliRunner().invoke(main, ["--json", *args])

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error == {
        "status": "error",
        "code": error_type.code,
        "message": "resolution failed",
    }


@pytest.mark.parametrize(
    "args, guarded_operation",
    [
        (["session", "kill", "abc"], "delete_session"),
        (["kernel", "interrupt", "abc"], "get_kernel_id_for_session"),
        (["kernel", "restart", "abc"], "get_kernel_id_for_session"),
        (["exec", "abc", "--code", "pass"], "get_kernel_id_for_session"),
        (["vars", "abc"], "get_kernel_id_for_session"),
    ],
)
def test_commands_abort_before_operation_for_ambiguous_selector(
    monkeypatch, args, guarded_operation
):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)

    def operation_must_not_run(*args, **kwargs):
        raise AssertionError("command ran after an ambiguous selector")

    monkeypatch.setattr(ServerClient, guarded_operation, operation_must_not_run)
    result = CliRunner().invoke(main, ["--json", *args])

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error["code"] == "SESSION_SELECTOR_AMBIGUOUS"
    assert "matches multiple active sessions" in error["message"]


def test_session_kill_resolves_name_to_full_id(monkeypatch):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    deleted = []
    monkeypatch.setattr(
        ServerClient,
        "delete_session",
        lambda self, session_id: deleted.append(session_id),
    )

    result = CliRunner().invoke(main, ["session", "kill", "analysis"])

    assert result.exit_code == 0, result.output
    assert deleted == ["abc1-session-id"]
    assert result.output.strip() == "Killed session abc1"


@pytest.mark.parametrize("use_json", [False, True])
def test_session_create_returns_shortest_unique_selector(monkeypatch, use_json):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    monkeypatch.setattr(
        ServerClient,
        "create_session",
        lambda self, kernel, name: {
            "session_id": "abc3-session-id",
            "kernel_id": "kernel-four",
            "kernel_name": kernel,
        },
    )
    args = ["--json"] if use_json else []

    result = CliRunner().invoke(
        main, [*args, "session", "create", "--kernel", "python3"]
    )

    assert result.exit_code == 0, result.output
    if use_json:
        data = json.loads(result.output)
        assert data["session_id"] == "abc3-session-id"
        assert data["session_selector"] == "abc3"
    else:
        assert result.output.startswith("Created session abc3 ")


def test_session_kill_accepts_multiple_selectors(monkeypatch):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    deleted = []
    monkeypatch.setattr(
        ServerClient,
        "delete_session",
        lambda self, session_id: deleted.append(session_id),
    )

    result = CliRunner().invoke(main, ["--json", "session", "kill", "analysis", "xyz"])

    assert result.exit_code == 0, result.output
    assert deleted == ["abc1-session-id", "xyz3-session-id"]
    assert json.loads(result.output) == {
        "status": "ok",
        "session_ids": ["abc1-session-id", "xyz3-session-id"],
        "session_selectors": ["abc1", "xyz"],
        "_human": "Killed sessions abc1, xyz",
    }


def test_session_kill_resolves_all_selectors_before_deleting(monkeypatch):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)

    def operation_must_not_run(*args, **kwargs):
        raise AssertionError("command deleted a session before resolving all selectors")

    monkeypatch.setattr(ServerClient, "delete_session", operation_must_not_run)

    result = CliRunner().invoke(
        main, ["--json", "session", "kill", "analysis", "missing"]
    )

    assert result.exit_code == 1
    error = json.loads(result.output)
    assert error["code"] == "SESSION_NOT_FOUND"


@pytest.mark.parametrize("command", ["interrupt", "restart"])
def test_kernel_commands_resolve_short_id_to_full_id(monkeypatch, command):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    resolved = []
    monkeypatch.setattr(
        ServerClient,
        "get_kernel_id_for_session",
        lambda self, session_id: resolved.append(session_id) or "kernel-one",
    )
    monkeypatch.setattr(
        ServerClient,
        f"{command}_kernel",
        lambda self, kernel_id: None,
    )

    result = CliRunner().invoke(main, ["kernel", command, "abc1"])

    assert result.exit_code == 0, result.output
    assert resolved == ["abc1-session-id"]
    assert "session abc1" in result.output


@pytest.mark.parametrize("command", ["interrupt", "restart"])
def test_kernel_commands_json_include_session_selector(monkeypatch, command):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    monkeypatch.setattr(
        ServerClient,
        "get_kernel_id_for_session",
        lambda self, session_id: "kernel-one",
    )
    monkeypatch.setattr(ServerClient, f"{command}_kernel", lambda self, kernel_id: None)

    result = CliRunner().invoke(main, ["--json", "kernel", command, "analysis"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["session_id"] == "abc1-session-id"
    assert data["session_selector"] == "abc1"
    assert data["kernel_id"] == "kernel-one"


def test_exec_and_vars_resolve_name_to_full_id(monkeypatch):
    monkeypatch.setattr(ServerClient, "list_sessions", _list_sessions)
    resolved = []
    monkeypatch.setattr(
        ServerClient,
        "get_kernel_id_for_session",
        lambda self, session_id: resolved.append(session_id) or "kernel-one",
    )
    monkeypatch.setattr(
        "jupyter_jcli.kernel.execute_code", lambda *args: {"outputs": []}
    )
    monkeypatch.setattr(
        "jupyter_jcli.kernel.kernel_connection", lambda *args: nullcontext(object())
    )
    monkeypatch.setattr(
        "jupyter_jcli.variables.list_variables",
        lambda kernel, timeout: {"source": VariableSource.FALLBACK, "variables": []},
    )

    runner = CliRunner()
    exec_result = runner.invoke(main, ["--json", "exec", "analysis", "--code", "pass"])
    vars_result = runner.invoke(main, ["--json", "vars", "analysis"])

    assert exec_result.exit_code == 0, exec_result.output
    assert vars_result.exit_code == 0, vars_result.output
    assert json.loads(vars_result.output)["session_id"] == "abc1-session-id"
    assert json.loads(vars_result.output)["session_selector"] == "abc1"
    assert resolved == ["abc1-session-id", "abc1-session-id"]
