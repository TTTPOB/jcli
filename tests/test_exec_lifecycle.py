"""Real-server regressions for fresh connections, deadlines, and SIGINT."""

import signal
import subprocess
import sys
import time

import pytest

from tests.helpers import wait_for_kernel_state


class TestFreshConnectionExec:
    """Fresh kernel_connection + immediate execute — must succeed reliably.

    Before the fix, this had a timing-dependent race: the client could send
    execute_request before the server's nudge() completed, causing the
    execute_reply to be dropped.
    """

    def test_exec_code_fresh_connection(self, jupyter_server):
        """Create a fresh session, open a brand-new connection, execute immediately."""
        import json

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        runner = CliRunner()

        # Create a session
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
            ],
        )
        assert result.exit_code == 0, f"session create failed: {result.output}"
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            # Execute code immediately - this exercises the race window.
            # With the ready probe fix, this must complete quickly.
            result = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--code",
                    "print('fresh-ok')",
                    "--timeout",
                    "30",
                ],
            )
            assert result.exit_code == 0, f"exec failed: {result.output}"
            assert "fresh-ok" in result.output
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

    def test_exec_file_fresh_connection(self, jupyter_server, tmp_path):
        """Fresh connection + file-based exec must also succeed."""
        import json
        import textwrap

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        runner = CliRunner()

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
            ],
        )
        assert result.exit_code == 0, f"session create failed: {result.output}"
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            script = tmp_path / "race_test.py"
            script.write_text(
                textwrap.dedent("""\
                # %%
                x = 1
                print(f"x={x}")
            """)
            )

            result = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--file",
                    str(script),
                    "--timeout",
                    "30",
                ],
            )
            assert result.exit_code == 0, f"exec failed: {result.output}"
            assert "x=1" in result.output
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

    def test_multi_exec_fresh_connections(self, jupyter_server):
        """Multiple fresh connections in a row — stress the race window.

        Each call opens a brand-new WebSocket.  If the fix works, every one
        of these must succeed.  Before the fix, the race condition could
        cause intermittent failures depending on server timing.
        """
        import json

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        runner = CliRunner()

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
            ],
        )
        assert result.exit_code == 0, "session create failed"
        sid = json.loads(result.output)["session_id"]

        try:
            for iteration in range(5):
                result = runner.invoke(
                    main,
                    [
                        "-s",
                        jupyter_server["url"],
                        "-t",
                        jupyter_server["token"],
                        "exec",
                        sid,
                        "--code",
                        f"print('iter-{iteration}')",
                        "--timeout",
                        "30",
                    ],
                )
                assert result.exit_code == 0, (
                    f"exec failed at iter {iteration}: {result.output}"
                )
                assert f"iter-{iteration}" in result.output
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


class TestExecutionTimeoutIntegration:
    def test_timeout_interrupts_kernel_and_preserves_session(
        self, jupyter_server, caplog
    ):
        import json

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        runner = CliRunner()
        created = runner.invoke(
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
        assert created.exit_code == 0, created.output
        sid = json.loads(created.output)["session_id"]

        try:
            started = time.monotonic()
            timed_out = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "--json",
                    "exec",
                    sid,
                    "--code",
                    "import time; print('inline partial', flush=True); time.sleep(30); timeout_sentinel = True",
                    "--timeout",
                    "1",
                ],
            )
            elapsed = time.monotonic() - started

            assert timed_out.exit_code == 1, timed_out.output
            decoder = json.JSONDecoder()
            first, offset = decoder.raw_decode(timed_out.output)
            error = json.loads(timed_out.output[offset:])
            assert first["status"] == "error"
            assert "inline partial" in str(first["outputs"])
            assert error["code"] == "TIMEOUT"
            assert "returned to idle" in error["message"]
            assert elapsed < 10

            recovered = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--code",
                    "print('recovered', 'timeout_sentinel' in globals())",
                    "--timeout",
                    "10",
                ],
            )
            assert recovered.exit_code == 0, recovered.output
            assert "recovered False" in recovered.output
            assert "Failed to stop websocket connection thread" not in caplog.text
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

    def test_file_cell_timeout_interrupts_kernel(self, jupyter_server, tmp_path):
        import json

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        script = tmp_path / "timeout_cell.py"
        script.write_text(
            "# %%\nimport time\n"
            "if globals().get('file_ran_before'):\n"
            "    print('new partial output', flush=True)\n"
            "    time.sleep(30)\n"
            "    file_timeout_sentinel = True\n"
            "else:\n"
            "    print('old successful output')\n"
            "    file_ran_before = True\n"
        )
        runner = CliRunner()
        created = runner.invoke(
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
        assert created.exit_code == 0, created.output
        sid = json.loads(created.output)["session_id"]

        try:
            arguments = [
                "-s",
                jupyter_server["url"],
                "-t",
                jupyter_server["token"],
                "--json",
                "exec",
                sid,
                "--file",
                str(script),
                "--cell",
                "0",
                "--timeout",
                "1",
            ]
            first = runner.invoke(main, arguments)
            assert first.exit_code == 0, first.output
            timed_out = runner.invoke(main, arguments)

            assert timed_out.exit_code == 1, timed_out.output
            lines = [json.loads(line) for line in timed_out.output.splitlines()]
            assert lines[-1]["code"] == "TIMEOUT"
            assert all(line.get("summary") is None for line in lines)
            assert lines[0]["status"] == "error"
            assert "new partial output" in str(lines[0]["cell"]["outputs"])
            notebook = json.loads(script.with_suffix(".ipynb").read_text())
            assert "new partial output" in str(notebook["cells"][0]["outputs"])
            assert "old successful output" not in str(
                [
                    output
                    for output in notebook["cells"][0]["outputs"]
                    if output["output_type"] == "stream"
                ]
            )

            recovered = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--code",
                    "print('file-recovered', 'file_timeout_sentinel' in globals())",
                    "--timeout",
                    "10",
                ],
            )
            assert recovered.exit_code == 0, recovered.output
            assert "file-recovered False" in recovered.output
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


# ---------------------------------------------------------------------------
# Signal handler tests - SIGINT -> kernel interrupt
# ---------------------------------------------------------------------------


class TestSigintHandlerIntegration:
    """Integration tests: SIGINT during exec interrupts the remote kernel."""

    def test_sigint_interrupts_long_running_code(self, jupyter_server):
        """Sending SIGINT to jcli exec interrupts the kernel."""
        import json

        from click.testing import CliRunner

        from jupyter_jcli.cli import main

        runner = CliRunner()

        # Create a session
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
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            # Run a long execution as a subprocess
            jcli_bin = str(__import__("pathlib").Path(sys.executable).parent / "j-cli")
            proc = subprocess.Popen(
                [
                    jcli_bin,
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--code",
                    "import time; time.sleep(30)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            wait_for_kernel_state(jupyter_server, sid, "busy")

            # Verify the process is still running (kernel is busy)
            if proc.poll() is not None:
                _, stderr = proc.communicate()
                pytest.fail(
                    f"process exited early with code {proc.returncode}. "
                    f"stderr: {stderr.decode() if stderr else 'none'}"
                )

            # Send SIGINT
            proc.send_signal(signal.SIGINT)

            try:
                _stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                _stdout, stderr = proc.communicate()
                pytest.fail(
                    f"Process did not exit after SIGINT. stderr: {stderr.decode()}"
                )

            # Should exit with 128 + SIGINT(2) = 130
            # (may also be -2 on some platforms for signal-terminated processes)
            assert proc.returncode in (130, -2), (
                f"expected exit code 130 or -2, got {proc.returncode}. stderr: {stderr.decode() if stderr else 'none'}"
            )

            # Kernel should recover (not stuck in "busy" forever).
            wait_for_kernel_state(jupyter_server, sid, "idle")
            result2 = runner.invoke(
                main,
                [
                    "-s",
                    jupyter_server["url"],
                    "-t",
                    jupyter_server["token"],
                    "exec",
                    sid,
                    "--code",
                    "print('recovered')",
                    "--timeout",
                    "15",
                ],
            )
            assert result2.exit_code == 0, (
                f"kernel did not recover after interrupt: exit_code={result2.exit_code} output={result2.output}"
            )
            assert "recovered" in result2.output

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
