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

from unittest.mock import patch


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
