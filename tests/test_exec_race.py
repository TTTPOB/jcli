"""Tests for the nudge() race condition.

Server-side ``nudge()`` must complete before ZMQ→WebSocket forwarding
(``handle_outgoing_message`` via ``subscribe`` callback) is set up.
If the client sends ``execute_request`` before nudge completes, the
kernel's reply arrives on the main shell ZMQ channel which has no
forwarding callback yet - the reply is silently dropped and the client
hangs in ``_message_received.wait()`` / ``_recv_reply()`` forever.

Fix: ``kernel_connection()`` now performs its own shell-channel round-trip
probe on the current WebSocket after ``kernel.start()``.  A matching
``kernel_info_reply`` proves the server pipeline is forwarding replies before
the client sends ``execute_request``.  If a specific WebSocket wedges during
server-side nudge setup, j-cli closes it and retries with a fresh WebSocket.
The retry is safe because no user code is sent until the probe succeeds.
"""

import queue
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Unit tests — verify the fix is in place
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self, messages=None):
        self.messages = queue.Queue()
        for msg in messages or []:
            self.messages.put(msg)

    def get_msg(self, timeout=None):
        return self.messages.get(timeout=timeout)


class _FakeClient:
    def __init__(self, shell_messages=None, iopub_messages=None):
        self.shell_channel = _FakeChannel(shell_messages)
        self.iopub_channel = _FakeChannel(iopub_messages)
        self.sent_msg_ids = []
        self.handled_kernel_info_reply = None

    def kernel_info(self):
        msg_id = f"probe-{len(self.sent_msg_ids)}"
        self.sent_msg_ids.append(msg_id)
        return msg_id

    def _handle_kernel_info_reply(self, msg):
        self.handled_kernel_info_reply = msg


class TestKernelWebsocketReadyProbe:
    """Verify kernel_connection waits for a current-WebSocket shell round-trip."""

    def test_kernel_connection_calls_ready_probe(self):
        """kernel_connection uses j-cli's ready probe after start."""
        from jupyter_jcli.kernel import kernel_connection

        with (
            patch("jupyter_jcli.kernel.KernelClient") as MockClient,
            patch("jupyter_jcli.kernel._wait_for_kernel_websocket_ready") as mock_ready,
        ):
            mock_instance = MockClient.return_value

            with kernel_connection("http://x", "tok", "kid") as k:
                assert k is mock_instance

            mock_instance.start.assert_called_once_with(timeout=10)
            mock_ready.assert_called_once_with(mock_instance, timeout=10)

    def test_kernel_stopped_when_ready_probe_fails(self):
        """kernel.stop() must be called even if the ready probe raises."""
        from jupyter_jcli.kernel import kernel_connection

        with (
            patch("jupyter_jcli.kernel.KernelClient") as MockClient,
            patch(
                "jupyter_jcli.kernel._wait_for_kernel_websocket_ready",
                side_effect=TimeoutError("kernel not ready"),
            ),
        ):
            mock_instance = MockClient.return_value
            mock_stop = mock_instance.stop

            with pytest.raises(TimeoutError, match="Kernel didn't respond in 30 seconds"):
                with kernel_connection("http://x", "tok", "kid"):
                    pass

            assert mock_stop.call_count == 3

    def test_kernel_connection_retries_fresh_websocket_after_probe_timeout(self):
        """A wedged WebSocket should not poison the whole exec attempt."""
        from jupyter_jcli.kernel import kernel_connection

        first_kernel = MagicMock()
        second_kernel = MagicMock()

        with (
            patch("jupyter_jcli.kernel.KernelClient") as MockClient,
            patch("jupyter_jcli.kernel._wait_for_kernel_websocket_ready") as mock_ready,
        ):
            MockClient.side_effect = [first_kernel, second_kernel]
            mock_ready.side_effect = [TimeoutError("wedged websocket"), None]

            with kernel_connection("http://x", "tok", "kid") as kernel:
                assert kernel is second_kernel

            first_kernel.start.assert_called_once_with(timeout=10)
            second_kernel.start.assert_called_once_with(timeout=10)
            first_kernel.stop.assert_called_once()
            second_kernel.stop.assert_called_once()

    def test_ready_probe_accepts_only_matching_kernel_info_reply(self):
        """The probe ignores stale/unrelated shell messages before succeeding."""
        from jupyter_jcli.kernel import _wait_for_kernel_websocket_ready

        matching_reply = {
            "msg_type": "kernel_info_reply",
            "parent_header": {"msg_id": "probe-0"},
            "content": {"protocol_version": "5.3"},
        }
        client = _FakeClient(
            shell_messages=[
                {"msg_type": "execute_reply", "parent_header": {"msg_id": "old"}},
                {"msg_type": "kernel_info_reply", "parent_header": {"msg_id": "stale"}},
                matching_reply,
            ],
            iopub_messages=[{"msg_type": "status"}],
        )
        kernel = MagicMock()
        kernel._manager.client = client

        _wait_for_kernel_websocket_ready(kernel, timeout=1)

        assert client.sent_msg_ids == ["probe-0"]
        assert client.handled_kernel_info_reply is matching_reply
        assert client.iopub_channel.messages.empty()

    def test_ready_probe_times_out_without_matching_reply(self):
        """The probe fails clearly if shell forwarding never becomes ready."""
        from jupyter_jcli.kernel import _wait_for_kernel_websocket_ready

        client = _FakeClient()
        kernel = MagicMock()
        kernel._manager.client = client

        with pytest.raises(TimeoutError, match="Kernel didn't respond in 0.01 seconds"):
            _wait_for_kernel_websocket_ready(kernel, timeout=0.01)

        assert client.sent_msg_ids == ["probe-0"]


# ---------------------------------------------------------------------------
# Execution deadline tests
# ---------------------------------------------------------------------------

class _BlockingKernel:
    def __init__(self, interrupt_error=None):
        self.interrupt_error = interrupt_error
        self.interrupt_calls = 0
        self.released = threading.Event()

    def execute(self, code, timeout=None, **kwargs):
        assert timeout is None
        if not self.released.wait(2):
            raise RuntimeError("test kernel was not interrupted")
        return {"status": "error", "outputs": [], "execution_count": 1}

    def interrupt(self, timeout=2):
        self.interrupt_calls += 1
        self.released.set()
        if self.interrupt_error is not None:
            raise self.interrupt_error


class TestExecutionTimeoutUnit:
    def test_completed_execution_does_not_interrupt(self):
        from jupyter_jcli.kernel import execute_with_timeout

        kernel = MagicMock()
        kernel.execute.return_value = {
            "status": "ok", "outputs": [], "execution_count": 1,
        }

        result = execute_with_timeout(kernel, "pass", timeout=1)

        assert result["status"] == "ok"
        kernel.execute.assert_called_once_with("pass", timeout=None)
        kernel.interrupt.assert_not_called()

    def test_deadline_interrupts_and_reports_timeout(self):
        from jupyter_jcli.kernel import ExecutionTimeout, execute_with_timeout

        kernel = _BlockingKernel()

        with pytest.raises(ExecutionTimeout, match="interrupted and returned to idle"):
            execute_with_timeout(kernel, "long_running()", timeout=0.01)

        assert kernel.interrupt_calls == 1

    def test_interrupt_failure_is_distinct(self):
        from jupyter_jcli.kernel import KernelInterruptFailed, execute_with_timeout

        kernel = _BlockingKernel(interrupt_error=OSError("server unavailable"))

        with pytest.raises(KernelInterruptFailed, match="server unavailable"):
            execute_with_timeout(kernel, "long_running()", timeout=0.01)

        assert kernel.interrupt_calls == 1


# ---------------------------------------------------------------------------
# Integration tests - real Jupyter server
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
            # Execute code immediately - this exercises the race window.
            # With the ready probe fix, this must complete quickly.
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


class TestExecutionTimeoutIntegration:
    def test_timeout_interrupts_kernel_and_preserves_session(self, jupyter_server):
        import json
        from click.testing import CliRunner
        from jupyter_jcli.cli import main

        runner = CliRunner()
        created = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "create", "--kernel", "python3",
        ])
        assert created.exit_code == 0, created.output
        sid = json.loads(created.output)["session_id"]

        try:
            started = time.monotonic()
            timed_out = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", sid, "--code",
                "import time; time.sleep(30); timeout_sentinel = True",
                "--timeout", "1",
            ])
            elapsed = time.monotonic() - started

            assert timed_out.exit_code == 1, timed_out.output
            error = json.loads(timed_out.output)
            assert error["code"] == "TIMEOUT"
            assert "returned to idle" in error["message"]
            assert elapsed < 10

            recovered = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", sid, "--code",
                "print('recovered', 'timeout_sentinel' in globals())",
                "--timeout", "10",
            ])
            assert recovered.exit_code == 0, recovered.output
            assert "recovered False" in recovered.output
        finally:
            runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "session", "kill", sid,
            ])

    def test_file_cell_timeout_interrupts_kernel(self, jupyter_server, tmp_path):
        import json
        from click.testing import CliRunner
        from jupyter_jcli.cli import main

        script = tmp_path / "timeout_cell.py"
        script.write_text(
            "# %%\n"
            "import time\n"
            "time.sleep(30)\n"
            "file_timeout_sentinel = True\n"
        )
        runner = CliRunner()
        created = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "session", "create", "--kernel", "python3",
        ])
        assert created.exit_code == 0, created.output
        sid = json.loads(created.output)["session_id"]

        try:
            timed_out = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "exec", sid, "--file", str(script), "--cell", "0",
                "--timeout", "1",
            ])

            assert timed_out.exit_code == 1, timed_out.output
            assert json.loads(timed_out.output)["code"] == "TIMEOUT"

            recovered = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "exec", sid, "--code",
                "print('file-recovered', 'file_timeout_sentinel' in globals())",
                "--timeout", "10",
            ])
            assert recovered.exit_code == 0, recovered.output
            assert "file-recovered False" in recovered.output
        finally:
            runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "session", "kill", sid,
            ])


# ---------------------------------------------------------------------------
# Signal handler tests - SIGINT -> kernel interrupt
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

        with (
            patch("jupyter_jcli.kernel.KernelClient"),
            patch("jupyter_jcli.kernel._wait_for_kernel_websocket_ready"),
            patch("jupyter_jcli.kernel.signal.signal") as mock_signal_fn,
        ):
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
