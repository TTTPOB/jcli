"""Test kernel interrupt and restart commands."""

import json

from click.testing import CliRunner

from jupyter_jcli.cli import main


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token,
        "--json", "session", "create", "--kernel", "python3",
    ])
    return json.loads(result.output)


def _kill_session(runner, url, token, session_id):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", session_id])


def test_kernel_interrupt(jupyter_server):
    runner = CliRunner()
    info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])

    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "kernel", "interrupt", info["session_id"],
    ])
    assert result.exit_code == 0
    assert "Interrupted" in result.output

    _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])


def test_kernel_restart(jupyter_server):
    runner = CliRunner()
    info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])

    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "kernel", "restart", info["session_id"],
    ])
    assert result.exit_code == 0
    assert "Restarted" in result.output

    _kill_session(runner, jupyter_server["url"], jupyter_server["token"], info["session_id"])
