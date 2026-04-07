"""Test session management commands."""

import json

from click.testing import CliRunner

from jcli.cli import main


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
