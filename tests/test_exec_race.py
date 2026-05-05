"""Tests for the nudge() race condition.

Server-side ``nudge()`` must complete before ZMQ→WebSocket forwarding
(``handle_outgoing_message`` via ``subscribe`` callback) is set up.
If the client sends ``execute_request`` before nudge completes, the
kernel's reply arrives on the main shell ZMQ channel which has no
forwarding callback yet — the reply is silently dropped and the client
hangs in ``_message_received.wait()`` / ``_recv_reply()`` forever.

Fix: ``kernel_connection()`` now calls ``wait_for_ready()`` after
``kernel.start()`` to ensure the server pipeline is ready before yielding.
"""

import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Unit tests — verify the fix is in place
# ---------------------------------------------------------------------------

class TestWaitForReadyCalled:
    """Verify kernel_connection calls wait_for_ready after start."""

    def test_execute_code_calls_wait_for_ready(self):
        """execute_code uses kernel_connection which calls wait_for_ready."""
        from unittest.mock import MagicMock
        from jupyter_jcli.kernel import kernel_connection

        with patch("jupyter_jcli.kernel.KernelClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_wait = MagicMock()
            mock_instance._manager.client.wait_for_ready = mock_wait

            with kernel_connection("http://x", "tok", "kid") as k:
                pass

            mock_wait.assert_called_once_with(timeout=30)


# ---------------------------------------------------------------------------
# Integration tests — real Jupyter server
# ---------------------------------------------------------------------------

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
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "create", "--kernel", "python3",
        ])
        assert result.exit_code == 0, f"session create failed: {result.output}"
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            # Execute code immediately — this exercises the race window.
            # With the wait_for_ready fix, this must complete quickly.
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", sid, "--code", "print('fresh-ok')", "--timeout", "30",
            ])
            assert result.exit_code == 0, f"exec failed: {result.output}"
            assert "fresh-ok" in result.output
        finally:
            runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "session", "kill", sid,
            ])

    def test_exec_file_fresh_connection(self, jupyter_server, tmp_path):
        """Fresh connection + file-based exec must also succeed."""
        import json
        import textwrap
        from click.testing import CliRunner
        from jupyter_jcli.cli import main

        runner = CliRunner()

        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "create", "--kernel", "python3",
        ])
        assert result.exit_code == 0, f"session create failed: {result.output}"
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            script = tmp_path / "race_test.py"
            script.write_text(textwrap.dedent("""\
                # %%
                x = 1
                print(f"x={x}")
            """))

            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", sid, "--file", str(script), "--timeout", "30",
            ])
            assert result.exit_code == 0, f"exec failed: {result.output}"
            assert "x=1" in result.output
        finally:
            runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "session", "kill", sid,
            ])

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

        for iteration in range(5):
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "session", "create", "--kernel", "python3",
            ])
            assert result.exit_code == 0, f"session create failed at iter {iteration}"
            data = json.loads(result.output)
            sid = data["session_id"]

            try:
                result = runner.invoke(main, [
                    "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                    "exec", sid, "--code", f"print('iter-{iteration}')",
                    "--timeout", "30",
                ])
                assert result.exit_code == 0, f"exec failed at iter {iteration}: {result.output}"
                assert f"iter-{iteration}" in result.output
            finally:
                runner.invoke(main, [
                    "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                    "session", "kill", sid,
                ])


# ---------------------------------------------------------------------------
# Signal handler tests — SIGINT → kernel interrupt
# ---------------------------------------------------------------------------

class TestSigintHandlerUnit:
    """Unit tests for the SIGINT handler created by _make_interrupt_handler."""

    def test_handler_posts_to_interrupt_api(self):
        """Handler calls the kernel interrupt HTTP API."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen") as mock_urlopen,
            patch("jupyter_jcli.kernel.sys.exit") as mock_exit,
        ):
            handler = _make_interrupt_handler("http://srv:8888", "tok", "kid-1")
            handler(signal.SIGINT, None)

            # Verify the HTTP request was made
            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert req.full_url == "http://srv:8888/api/kernels/kid-1/interrupt"
            assert req.get_method() == "POST"
            assert req.get_header("Authorization") == "Bearer tok"
            assert call_args[1]["timeout"] == 2  # kwarg

            mock_exit.assert_called_once_with(128 + signal.SIGINT)

    def test_handler_second_signal_restores_default(self):
        """Second signal restores SIG_DFL before exit."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen"),
            patch("jupyter_jcli.kernel.sys.exit"),
            patch("jupyter_jcli.kernel.signal.signal") as mock_signal,
        ):
            handler = _make_interrupt_handler("http://srv:8888", "tok", "kid-1")
            handler(signal.SIGINT, None)

            mock_signal.assert_called_with(signal.SIGINT, signal.SIG_DFL)

    def test_handler_no_token(self):
        """Handler works without a token (no Authorization header)."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen") as mock_urlopen,
            patch("jupyter_jcli.kernel.sys.exit"),
        ):
            handler = _make_interrupt_handler("http://srv:8888", None, "kid-1")
            handler(signal.SIGINT, None)

            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") is None

    def test_handler_http_error_is_silent(self):
        """HTTP errors are silently ignored — exit still happens."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen",
                  side_effect=OSError("connection refused")),
            patch("jupyter_jcli.kernel.sys.exit") as mock_exit,
        ):
            handler = _make_interrupt_handler("http://srv:8888", "tok", "kid-1")
            handler(signal.SIGINT, None)

            # Must still exit even though the HTTP request failed
            mock_exit.assert_called_once()

    def test_signal_handler_set_and_restored(self):
        """kernel_connection sets and restores signal handlers."""
        from jupyter_jcli.kernel import kernel_connection

        with patch("jupyter_jcli.kernel.KernelClient"):
            with patch("jupyter_jcli.kernel.signal.signal") as mock_signal_fn:
                mock_signal_fn.return_value = "OLD_HANDLER"
                with kernel_connection("http://x", "tok", "kid"):
                    pass

                # Should have been called twice for setup (SIGINT, SIGTERM)
                # and twice for teardown
                setup_calls = [
                    c for c in mock_signal_fn.call_args_list
                    if c[0][0] == signal.SIGINT
                ]
                assert len(setup_calls) == 2  # one set, one restore
                assert len([c for c in mock_signal_fn.call_args_list
                            if c[0][0] == signal.SIGTERM]) == 2


class TestSigintHandlerIntegration:
    """Integration tests: SIGINT during exec interrupts the remote kernel."""

    def test_sigint_interrupts_long_running_code(self, jupyter_server):
        """Sending SIGINT to jcli exec interrupts the kernel."""
        import json
        from click.testing import CliRunner
        from jupyter_jcli.cli import main

        runner = CliRunner()

        # Create a session
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "create", "--kernel", "python3",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        sid = data["session_id"]

        try:
            # Run a long execution as a subprocess
            jcli_bin = str(
                __import__("pathlib").Path(sys.executable).parent / "j-cli"
            )
            proc = subprocess.Popen(
                [jcli_bin,
                 "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                 "exec", sid, "--code", "import time; time.sleep(30)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

            # Give the kernel time to enter the busy state
            time.sleep(3)

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
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                pytest.fail(f"Process did not exit after SIGINT. stderr: {stderr.decode()}")

            # Should exit with 128 + SIGINT(2) = 130
            # (may also be -2 on some platforms for signal-terminated processes)
            assert proc.returncode in (130, -2), \
                f"expected exit code 130 or -2, got {proc.returncode}. stderr: {stderr.decode() if stderr else 'none'}"

            # Kernel should recover (not stuck in "busy" forever)
            # Give it a moment to process the interrupt and for buffers to flush
            time.sleep(2)
            result2 = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", sid, "--code", "print('recovered')", "--timeout", "15",
            ])
            assert result2.exit_code == 0, \
                f"kernel did not recover after interrupt: exit_code={result2.exit_code} output={result2.output}"
            assert "recovered" in result2.output

        finally:
            runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "session", "kill", sid,
            ])
