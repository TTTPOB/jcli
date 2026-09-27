"""Test kernel interrupt and restart commands."""

import json
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from tests.helpers import wait_for_kernel_state


def _create_session(runner, url, token):
    result = runner.invoke(
        main,
        [
            "-s",
            url,
            "-t",
            token,
            "--json",
            "session",
            "create",
            "--kernel",
            "python3",
        ],
    )
    return json.loads(result.output)


def _kill_session(runner, url, token, session_id):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", session_id])


def _invoke(runner, jupyter_server, *args):
    return runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            *args,
        ],
    )


def test_kernel_restart_clears_variables_and_keeps_session_usable(jupyter_server):
    runner = CliRunner()
    info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
    session_id = info["session_id"]

    try:
        defined = _invoke(
            runner,
            jupyter_server,
            "exec",
            session_id,
            "--code",
            "restart_probe_var = 41",
            "--timeout",
            "30",
        )
        assert defined.exit_code == 0, defined.output

        result = _invoke(runner, jupyter_server, "kernel", "restart", session_id)
        assert result.exit_code == 0, result.output
        assert "Restarted" in result.output

        # The restarted kernel is only usable once the server reports it idle.
        wait_for_kernel_state(jupyter_server, session_id, "idle")

        checked = _invoke(
            runner,
            jupyter_server,
            "exec",
            session_id,
            "--code",
            "print('restart_probe_var' in globals(), 6 * 7)",
            "--timeout",
            "30",
        )
        assert checked.exit_code == 0, checked.output
        assert "False 42" in checked.output
    finally:
        _kill_session(
            runner, jupyter_server["url"], jupyter_server["token"], session_id
        )


def test_kernel_interrupt_stops_running_code_and_keeps_session_usable(
    jupyter_server, tmp_path
):
    runner = CliRunner()
    info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
    session_id = info["session_id"]
    proc = None
    started_file = tmp_path / "execution-started"

    try:
        # A real exec subprocess keeps the kernel busy in a long sleep; the
        # exec timeout is far above the sleep so only our interrupt can end it.
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "jupyter_jcli",
                "-s",
                jupyter_server["url"],
                "-t",
                jupyter_server["token"],
                "exec",
                session_id,
                "--code",
                (
                    "import time; from pathlib import Path; "
                    f"Path({str(started_file)!r}).touch(); time.sleep(60)"
                ),
                "--timeout",
                "120",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for user code, not the transient busy status of a readiness probe.
        deadline = time.monotonic() + 15
        while not started_file.exists():
            assert proc.poll() is None, "exec exited before user code started"
            if time.monotonic() >= deadline:
                pytest.fail("kernel did not start the interrupt target")
            time.sleep(0.05)

        result = _invoke(runner, jupyter_server, "kernel", "interrupt", session_id)
        assert result.exit_code == 0, result.output
        assert "Interrupted" in result.output

        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("exec subprocess kept running after kernel interrupt")
        assert proc.returncode == 1, (stdout, stderr)
        assert b"KeyboardInterrupt" in stdout + stderr

        wait_for_kernel_state(jupyter_server, session_id, "idle")

        recovered = _invoke(
            runner,
            jupyter_server,
            "exec",
            session_id,
            "--code",
            "print('interrupt-recovered')",
            "--timeout",
            "30",
        )
        assert recovered.exit_code == 0, recovered.output
        assert "interrupt-recovered" in recovered.output
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        _kill_session(
            runner, jupyter_server["url"], jupyter_server["token"], session_id
        )
