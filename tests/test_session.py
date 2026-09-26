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


def test_session_list_vars_preview_present(monkeypatch):
    """Default (without --no-vars) should include vars_preview in JSON output."""
    runner = CliRunner()

    sessions = [
        {
            "session_id": "session-1",
            "kernel_id": "kernel-1",
            "kernel_name": "python3",
            "kernel_state": "idle",
            "name": "",
        }
    ]
    monkeypatch.setattr(
        "jupyter_jcli.server.ServerClient.list_sessions", lambda _self: sessions
    )
    monkeypatch.setattr(
        "jupyter_jcli.commands.session._enrich_with_vars",
        lambda _ctx, items: items[0].update(
            vars_preview={"names": ["answer"], "total": 1}
        ),
    )

    result = runner.invoke(main, ["--json", "session", "list"])
    assert result.exit_code == 0
    assert json.loads(result.output)["sessions"][0]["vars_preview"] == {
        "names": ["answer"],
        "total": 1,
    }


def test_session_list_human_hint(monkeypatch):
    """Human output should include the hint line pointing at j-cli vars."""
    runner = CliRunner()

    sessions = [
        {
            "session_id": "session-1",
            "kernel_id": "kernel-1",
            "kernel_name": "python3",
            "kernel_state": "idle",
            "name": "",
        }
    ]
    monkeypatch.setattr(
        "jupyter_jcli.server.ServerClient.list_sessions", lambda _self: sessions
    )
    monkeypatch.setattr(
        "jupyter_jcli.commands.session._enrich_with_vars",
        lambda _ctx, items: items[0].update(vars_preview={"names": [], "total": 0}),
    )

    result = runner.invoke(main, ["session", "list"])
    assert result.exit_code == 0
    assert "j-cli vars" in result.output
