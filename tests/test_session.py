"""Test session management commands."""

import json

from click.testing import CliRunner

from jupyter_jcli.cli import main


def test_session_create_and_list(jupyter_server):
    runner = CliRunner()

    # Create
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "create", "--kernel", "python3", "--name", "test-sess",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["kernel_name"] == "python3"
    session_id = data["session_id"]

    # List
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "list",
    ])
    assert result.exit_code == 0
    sessions = json.loads(result.output)["sessions"]
    ids = [s["session_id"] for s in sessions]
    assert session_id in ids

    # Kill
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "session", "kill", session_id,
    ])
    assert result.exit_code == 0
    assert "Killed" in result.output


def test_session_create_human(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "session", "create", "--kernel", "python3",
    ])
    assert result.exit_code == 0
    assert "Created session" in result.output

    # Extract session_id from human output and clean up
    # Format: "Created session <id> (kernel: <kid>, spec: python3)"
    sid = result.output.split("Created session ")[1].split(" ")[0]
    runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "session", "kill", sid,
    ])


def test_session_list_empty(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "session", "list",
    ])
    assert result.exit_code == 0


def test_session_list_no_vars_flag(jupyter_server):
    """--no-vars should return session list without vars_preview key in JSON."""
    runner = CliRunner()

    # Create a session so the list is non-empty
    create_result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "create", "--kernel", "python3",
    ])
    assert create_result.exit_code == 0
    sid = json.loads(create_result.output)["session_id"]

    try:
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "list", "--no-vars",
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "sessions" in data
        # With --no-vars, no session should have a vars_preview key
        for s in data["sessions"]:
            assert "vars_preview" not in s
    finally:
        runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "session", "kill", sid,
        ])


def test_session_list_vars_preview_present(jupyter_server):
    """Default (without --no-vars) should include vars_preview in JSON output."""
    runner = CliRunner()

    create_result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "create", "--kernel", "python3",
    ])
    assert create_result.exit_code == 0
    sid = json.loads(create_result.output)["session_id"]

    try:
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "list",
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        sessions = data["sessions"]
        target = next((s for s in sessions if s["session_id"] == sid), None)
        assert target is not None
        assert "vars_preview" in target
        assert "names" in target["vars_preview"]
        assert "total" in target["vars_preview"]
    finally:
        runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "session", "kill", sid,
        ])


def test_session_list_human_hint(jupyter_server):
    """Human output should include the hint line pointing at j-cli vars."""
    runner = CliRunner()

    create_result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "create", "--kernel", "python3",
    ])
    assert create_result.exit_code == 0
    sid = json.loads(create_result.output)["session_id"]

    try:
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "session", "list",
        ])
        assert result.exit_code == 0
        assert "j-cli vars" in result.output
    finally:
        runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "session", "kill", sid,
        ])
