"""Test session management commands."""

import json

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.session import KernelState, _coerce_state

# ---------------------------------------------------------------------------
# KernelState enum behaviour
# ---------------------------------------------------------------------------


class TestKernelState:
    def test_members_exist(self):
        assert KernelState.IDLE == "idle"
        assert KernelState.BUSY == "busy"
        assert KernelState.STARTING == "starting"
        assert KernelState.DEAD == "dead"
        assert KernelState.UNKNOWN == "unknown"

    def test_str_inheritance(self):
        assert isinstance(KernelState.IDLE, str)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            KernelState("bogus")

    def test_coerce_known_value(self):
        assert _coerce_state("idle") is KernelState.IDLE
        assert _coerce_state("busy") is KernelState.BUSY

    def test_coerce_unknown_falls_back(self):
        assert _coerce_state("restarting") is KernelState.UNKNOWN
        assert _coerce_state("") is KernelState.UNKNOWN
        assert _coerce_state("some_future_state") is KernelState.UNKNOWN


def test_session_create_and_list(jupyter_server):
    runner = CliRunner()

    # Create
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "--json",
            "session",
            "create",
            "--kernel",
            "python3",
            "--name",
            "test-sess",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["kernel_name"] == "python3"
    session_id = data["session_id"]
    assert session_id.startswith(data["session_selector"])
    assert len(data["session_selector"]) >= 3

    # List
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "--json",
            "session",
            "list",
        ],
    )
    assert result.exit_code == 0
    sessions = json.loads(result.output)["sessions"]
    ids = [s["session_id"] for s in sessions]
    assert session_id in ids
    created_session = next(s for s in sessions if s["session_id"] == session_id)
    assert created_session["session_selector"] == data["session_selector"]

    # Kill
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "session",
            "kill",
            session_id,
        ],
    )
    assert result.exit_code == 0
    assert "Killed" in result.output


def test_session_create_is_new_and_name_conflicts(jupyter_server):
    runner = CliRunner()
    base = ["-s", jupyter_server["url"], "-t", jupyter_server["token"], "--json"]
    created = []

    def create(*options):
        result = runner.invoke(main, [*base, "session", "create", *options])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    try:
        first = create("--kernel", "python3")
        created.append(first["session_id"])
        second = create("--kernel", "python3")
        created.append(second["session_id"])
        assert first["session_id"] != second["session_id"]
        assert first["kernel_id"] != second["kernel_id"]

        named = create("--kernel", "python3", "--name", "unique-test-name")
        created.append(named["session_id"])
        conflict = runner.invoke(
            main,
            [
                *base,
                "session",
                "create",
                "--kernel",
                "python3",
                "--name",
                "unique-test-name",
            ],
        )
        assert conflict.exit_code != 0
        error = json.loads(conflict.output)
        assert error["code"] == "SESSION_NAME_CONFLICT"
        assert named["session_selector"] in error["message"]

        invalid = runner.invoke(
            main,
            [*base, "session", "create", "--kernel", "nonexistent-jcli-kernel"],
        )
        assert invalid.exit_code != 0
        assert json.loads(invalid.output)["code"] == "SESSION_CREATE_FAILED"
    finally:
        for session_id in created:
            runner.invoke(main, [*base, "session", "kill", session_id])


def test_session_create_human(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "session",
            "create",
            "--kernel",
            "python3",
        ],
    )
    assert result.exit_code == 0
    assert "Created session" in result.output

    # Extract the session selector from human output and clean up
    # Format: "Created session <selector> (kernel: <kid>, spec: python3)"
    sid = result.output.split("Created session ")[1].split(" ")[0]
    runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "session",
            "kill",
            sid,
        ],
    )


def test_session_list_empty(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "session",
            "list",
        ],
    )
    assert result.exit_code == 0
    assert result.output.strip() == "No active sessions"


def test_session_list_no_vars_flag(jupyter_server):
    """--no-vars should return session list without vars_preview key in JSON."""
    runner = CliRunner()

    # Create a session so the list is non-empty
    create_result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "--json",
            "session",
            "create",
            "--kernel",
            "python3",
        ],
    )
    assert create_result.exit_code == 0
    sid = json.loads(create_result.output)["session_id"]

    try:
        result = runner.invoke(
            main,
            [
                "-s",
                jupyter_server["url"],
                "-t",
                jupyter_server["token"],
                "--json",
                "session",
                "list",
                "--no-vars",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "sessions" in data
        target = next(
            (item for item in data["sessions"] if item["session_id"] == sid), None
        )
        assert target is not None
        assert "vars_preview" not in target
    finally:
        runner.invoke(
            main,
            [
                "-s",
                jupyter_server["url"],
                "-t",
                jupyter_server["token"],
                "session",
                "kill",
                sid,
            ],
        )


def _session(session_id: str, kernel_id: str, state: str = "idle") -> dict:
    return {
        "session_id": session_id,
        "kernel_id": kernel_id,
        "kernel_name": "python3",
        "kernel_state": state,
        "name": "",
    }


def _install_vars_stubs(monkeypatch, sessions, list_variables):
    """Patch the kernel/list_variables boundary; _enrich_with_vars runs for real."""
    import contextlib

    opened: list[str] = []
    call_log: list[str] = []

    monkeypatch.setattr(
        "jupyter_jcli.server.ServerClient.list_sessions", lambda _self: sessions
    )

    @contextlib.contextmanager
    def fake_connection(_url, _token, kernel_id):
        opened.append(kernel_id)
        yield kernel_id

    def recording_list_variables(kernel, *, timeout):
        call_log.append(kernel)
        return list_variables(kernel, timeout=timeout)

    monkeypatch.setattr(
        "jupyter_jcli.kernel.kernel_connection", fake_connection, raising=True
    )
    monkeypatch.setattr(
        "jupyter_jcli.variables.list_variables", recording_list_variables, raising=True
    )
    return opened, call_log


def test_session_list_vars_preview_truncates_names_and_reports_total(monkeypatch):
    """vars_preview keeps the first names and still reports the full total."""
    runner = CliRunner()
    sessions = [_session("session-alpha", "kernel-alpha")]
    variables = [{"name": f"var{i}", "type": "int", "value": str(i)} for i in range(7)]
    opened, call_log = _install_vars_stubs(
        monkeypatch,
        sessions,
        lambda _kernel, *, timeout: {"variables": variables, "source": "fallback"},
    )

    result = runner.invoke(main, ["--json", "session", "list"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["sessions"][0]["vars_preview"] == {
        "names": ["var0", "var1", "var2", "var3", "var4"],
        "total": 7,
    }
    assert opened == ["kernel-alpha"]
    assert call_log == ["kernel-alpha"]


def test_session_list_vars_preview_skips_busy_dead_unknown(monkeypatch):
    """Non-idle kernels are never queried for variables."""
    runner = CliRunner()
    sessions = [
        _session("session-alpha", "kernel-alpha"),
        _session("session-beta", "kernel-beta", "busy"),
        _session("session-gamma", "kernel-gamma", "dead"),
        _session("session-delta", "kernel-delta", "unknown"),
    ]
    opened, call_log = _install_vars_stubs(
        monkeypatch,
        sessions,
        lambda _kernel, *, timeout: {"variables": [], "source": "fallback"},
    )

    result = runner.invoke(main, ["--json", "session", "list"])

    assert result.exit_code == 0, result.output
    previews = {
        item["session_id"]: item["vars_preview"]
        for item in json.loads(result.output)["sessions"]
    }
    assert previews["session-alpha"] == {"names": [], "total": 0}
    for session_id in ("session-beta", "session-gamma", "session-delta"):
        assert previews[session_id] == {
            "names": [],
            "total": -1,
            "unavailable": True,
        }
    assert opened == ["kernel-alpha"]
    assert call_log == ["kernel-alpha"]


def test_session_list_vars_preview_isolates_single_session_failure(monkeypatch):
    """One failing kernel marks only its own session as unavailable."""
    runner = CliRunner()
    sessions = [
        _session("session-alpha", "kernel-alpha"),
        _session("session-beta", "kernel-beta"),
    ]

    def list_variables(kernel, *, timeout):
        if kernel == "kernel-alpha":
            raise RuntimeError("alpha kernel failed")
        return {
            "variables": [{"name": "answer", "type": "int", "value": "42"}],
            "source": "fallback",
        }

    opened, _call_log = _install_vars_stubs(monkeypatch, sessions, list_variables)

    result = runner.invoke(main, ["--json", "session", "list"])

    assert result.exit_code == 0, result.output
    previews = {
        item["session_id"]: item["vars_preview"]
        for item in json.loads(result.output)["sessions"]
    }
    assert previews["session-alpha"] == {
        "names": [],
        "total": -1,
        "unavailable": True,
    }
    assert previews["session-beta"] == {"names": ["answer"], "total": 1}
    # Order-independent: both kernels were queried by the thread pool.
    assert sorted(opened) == ["kernel-alpha", "kernel-beta"]


def test_session_list_human_hint(monkeypatch):
    """Human output should include the hint line pointing at j-cli vars."""
    runner = CliRunner()
    sessions = [_session("session-alpha", "kernel-alpha")]
    _install_vars_stubs(
        monkeypatch,
        sessions,
        lambda _kernel, *, timeout: {"variables": [], "source": "fallback"},
    )

    result = runner.invoke(main, ["session", "list"])

    assert result.exit_code == 0, result.output
    assert "j-cli vars" in result.output
