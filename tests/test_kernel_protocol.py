"""Deterministic kernel readiness, execution deadline, and signal contracts."""

import json
import queue
import signal
import socket
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


class _FakeChannel:
    def __init__(self, messages=None):
        self.messages = queue.Queue()
        for msg in messages or []:
            self.messages.put(msg)

    def get_msg(self, timeout=None):
        return self.messages.get(timeout=timeout)


class _FakeClock:
    """Injectable monotonic clock for deterministic ready-probe deadlines."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ProbeChannel:
    """Non-blocking ready-probe channel driven by an injected clock.

    Queued messages are returned immediately (no clock advance).  An empty
    channel advances the clock by the timeout a real blocking read would have
    consumed, then raises queue.Empty — so probe progress depends only on the
    fake clock, never on wall-clock time.
    """

    def __init__(self, messages=None, *, clock: _FakeClock) -> None:
        self.messages = list(messages or [])
        self.clock = clock
        self.get_calls = 0

    def get_msg(self, timeout=None):
        self.get_calls += 1
        if self.messages:
            return self.messages.pop(0)
        if timeout:
            self.clock.advance(timeout)
        raise queue.Empty


class _EndlessProbeChannel:
    """Always-ready channel that advances the injected clock on every read."""

    def __init__(self, clock: _FakeClock, *, step: float, message: dict) -> None:
        self.clock = clock
        self.step = step
        self.message = message
        self.get_calls = 0

    def get_msg(self, timeout=None):
        self.get_calls += 1
        self.clock.advance(self.step)
        return self.message


class _FakeClient:
    def __init__(self, shell_messages=None, iopub_messages=None, *, clock):
        self.shell_channel = _ProbeChannel(shell_messages, clock=clock)
        self.iopub_channel = _ProbeChannel(iopub_messages, clock=clock)
        self.sent_msg_ids = []
        self.handled_kernel_info_reply = None

    def kernel_info(self):
        msg_id = f"probe-{len(self.sent_msg_ids)}"
        self.sent_msg_ids.append(msg_id)
        return msg_id

    def _handle_kernel_info_reply(self, msg):
        self.handled_kernel_info_reply = msg


class TestKernelWebsocketReadyProbe:
    """Verify kernel_connection waits for shell and IOPub round-trips."""

    def _run_probe(self, client, clock, *, timeout=0.01, expect_timeout=False):
        from jupyter_jcli.kernel import _wait_for_kernel_websocket_ready

        kernel = MagicMock()
        kernel._manager.client = client
        fake_time = SimpleNamespace(monotonic=clock.monotonic)
        with patch("jupyter_jcli.kernel.time", fake_time):
            if expect_timeout:
                with pytest.raises(
                    TimeoutError,
                    match=f"Kernel didn't respond in {timeout:g} seconds",
                ):
                    _wait_for_kernel_websocket_ready(kernel, timeout=timeout)
            else:
                _wait_for_kernel_websocket_ready(kernel, timeout=timeout)

    def test_kernel_connection_calls_ready_probe(self):
        """kernel_connection uses j-cli's ready probe after start."""
        from jupyter_jcli.kernel import (
            _KERNEL_READY_ATTEMPT_TIMEOUT,
            _JCLIKernelWebSocketClient,
            kernel_connection,
        )

        with (
            patch("jupyter_jcli.kernel.KernelClient") as MockClient,
            patch("jupyter_jcli.kernel._wait_for_kernel_websocket_ready") as mock_ready,
        ):
            mock_instance = MockClient.return_value

            with kernel_connection("http://x", "tok", "kid") as k:
                assert k is mock_instance

            MockClient.assert_called_once_with(
                server_url="http://x",
                token="tok",
                kernel_id="kid",
                client_factory=_JCLIKernelWebSocketClient,
                client_kwargs={"timeout": _KERNEL_READY_ATTEMPT_TIMEOUT},
            )
            mock_instance.start.assert_called_once_with(
                timeout=_KERNEL_READY_ATTEMPT_TIMEOUT
            )
            mock_ready.assert_called_once_with(
                mock_instance, timeout=_KERNEL_READY_ATTEMPT_TIMEOUT
            )

    def test_websocket_shutdown_wakes_connection_thread(self):
        """Socket shutdown wakes the reader before stop_channels joins it."""
        from jupyter_jcli.kernel import _JCLIKernelWebSocketClient

        client = _JCLIKernelWebSocketClient(endpoint="ws://example.test/channels")
        kernel_socket = MagicMock()
        websocket = kernel_socket.sock
        raw_socket = websocket.sock
        client.kernel_socket = kernel_socket

        client.stop_channels()

        raw_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        websocket.shutdown.assert_called_once()
        kernel_socket.close.assert_not_called()
        assert kernel_socket.sock is None
        assert client.kernel_socket is None

    def test_websocket_dispatcher_poll_is_bounded(self):
        """The listener delegates to run_forever instead of spinning."""
        from jupyter_jcli.kernel import _JCLIKernelWebSocketClient

        client = _JCLIKernelWebSocketClient(endpoint="ws://example.test/channels")
        client.kernel_socket = MagicMock()

        client._run_websocket()

        client.kernel_socket.run_forever.assert_called_once()

    def test_kernel_stopped_when_ready_probe_fails(self):
        """kernel.stop() must be called even if the ready probe raises."""
        from jupyter_jcli.kernel import _KERNEL_READY_MAX_ATTEMPTS, kernel_connection

        with (
            patch("jupyter_jcli.kernel.KernelClient") as MockClient,
            patch(
                "jupyter_jcli.kernel._wait_for_kernel_websocket_ready",
                side_effect=TimeoutError("kernel not ready"),
            ),
        ):
            mock_instance = MockClient.return_value
            mock_stop = mock_instance.stop

            with (
                pytest.raises(
                    TimeoutError, match="Kernel didn't respond in 30 seconds"
                ),
                kernel_connection("http://x", "tok", "kid"),
            ):
                pass

            assert mock_stop.call_count == _KERNEL_READY_MAX_ATTEMPTS

    def test_kernel_connection_retries_fresh_websocket_after_probe_timeout(self):
        """A wedged WebSocket should not poison the whole exec attempt."""
        from jupyter_jcli.kernel import _KERNEL_READY_ATTEMPT_TIMEOUT, kernel_connection

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

            first_kernel.start.assert_called_once_with(
                timeout=_KERNEL_READY_ATTEMPT_TIMEOUT
            )
            second_kernel.start.assert_called_once_with(
                timeout=_KERNEL_READY_ATTEMPT_TIMEOUT
            )
            first_kernel.stop.assert_called_once()
            second_kernel.stop.assert_called_once()

    def test_ready_probe_accepts_matching_shell_reply_and_iopub_idle(self):
        """The probe ignores unrelated messages before both channels succeed."""
        clock = _FakeClock()
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
            iopub_messages=[
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": "stale"},
                    "content": {"execution_state": "idle"},
                },
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"execution_state": "busy"},
                },
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"execution_state": "idle"},
                },
            ],
            clock=clock,
        )

        self._run_probe(client, clock, timeout=1)

        assert client.sent_msg_ids == ["probe-0"]
        assert client.handled_kernel_info_reply is matching_reply
        assert client.iopub_channel.messages == []

    def test_ready_probe_times_out_without_matching_shell_reply(self):
        """The probe fails clearly if shell forwarding never becomes ready."""
        clock = _FakeClock()
        client = _FakeClient(clock=clock)

        self._run_probe(client, clock, expect_timeout=True)

        assert client.sent_msg_ids == ["probe-0"]

    def test_ready_probe_drains_iopub_backlog_before_timeout(self):
        """Queued unrelated traffic cannot hide the matching idle status."""
        clock = _FakeClock()
        client = _FakeClient(
            shell_messages=[
                {
                    "msg_type": "kernel_info_reply",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"protocol_version": "5.3"},
                }
            ],
            iopub_messages=[
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": f"stale-{index}"},
                    "content": {"execution_state": "idle"},
                }
                for index in range(20)
            ]
            + [
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"execution_state": "idle"},
                }
            ],
            clock=clock,
        )

        self._run_probe(client, clock)

        assert client.handled_kernel_info_reply is not None
        assert client.iopub_channel.messages == []

    def test_ready_probe_honors_timeout_while_iopub_remains_busy(self):
        """Continuous unrelated IOPub traffic cannot bypass the deadline."""
        clock = _FakeClock()
        client = _FakeClient(
            shell_messages=[
                {
                    "msg_type": "kernel_info_reply",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"protocol_version": "5.3"},
                }
            ],
            clock=clock,
        )
        client.iopub_channel = _EndlessProbeChannel(
            clock,
            step=0.004,
            message={
                "msg_type": "status",
                "parent_header": {"msg_id": "unrelated"},
                "content": {"execution_state": "idle"},
            },
        )

        self._run_probe(client, clock, expect_timeout=True)

        assert client.iopub_channel.get_calls > 1

    def test_ready_probe_times_out_without_iopub_idle(self):
        """A shell round-trip alone does not prove execution can complete."""
        clock = _FakeClock()
        client = _FakeClient(
            shell_messages=[
                {
                    "msg_type": "kernel_info_reply",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"protocol_version": "5.3"},
                }
            ],
            clock=clock,
        )

        self._run_probe(client, clock, expect_timeout=True)

        assert client.handled_kernel_info_reply is None

    def test_ready_probe_requires_both_channels_for_same_request(self):
        """Replies from different requests cannot jointly satisfy readiness."""
        clock = _FakeClock()
        client = _FakeClient(
            shell_messages=[
                {
                    "msg_type": "kernel_info_reply",
                    "parent_header": {"msg_id": "probe-0"},
                    "content": {"protocol_version": "5.3"},
                }
            ],
            iopub_messages=[
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": "other-request"},
                    "content": {"execution_state": "idle"},
                }
            ],
            clock=clock,
        )

        self._run_probe(client, clock, expect_timeout=True)

        assert client.handled_kernel_info_reply is None


# ---------------------------------------------------------------------------
# Execution deadline tests
# ---------------------------------------------------------------------------


def _kernel_message(msg_type, *, parent="execute-1", **content):
    return {
        "header": {"msg_type": msg_type},
        "parent_header": {"msg_id": parent},
        "content": content,
    }


class _ExecutionClient:
    def __init__(self, *, iopub_after_execute=None, shell_after_execute=None):
        self.iopub_channel = _FakeChannel()
        self.shell_channel = _FakeChannel()
        self.iopub_after_execute = iopub_after_execute or []
        self.shell_after_execute = shell_after_execute or []
        self.execute_calls = []

    def execute(self, code, **kwargs):
        self.execute_calls.append((code, kwargs))
        for msg in self.iopub_after_execute:
            self.iopub_channel.messages.put(msg)
        for msg in self.shell_after_execute:
            self.shell_channel.messages.put(msg)
        return "execute-1"


class _ContinuouslyReadyChannel:
    def get_msg(self, timeout=None):
        return _kernel_message("stream", parent="other-request", text="noise")


class _ExecutionKernel:
    def __init__(self, client, *, interrupt_messages=None, interrupt_error=None):
        self._manager = SimpleNamespace(client=client)
        self.interrupt_messages = interrupt_messages or []
        self.interrupt_error = interrupt_error
        self.interrupt_calls = 0

    def interrupt(self, timeout=2):
        self.interrupt_calls += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error
        for msg in self.interrupt_messages:
            self._manager.client.iopub_channel.messages.put(msg)


class TestExecutionTimeoutUnit:
    def test_completed_execution_preserves_result_and_kwargs(self):
        from jupyter_jcli.kernel import execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[
                _kernel_message(
                    "display_data",
                    data={"text/plain": "result"},
                    metadata={},
                    transient={"display_id": "temporary"},
                ),
                _kernel_message("status", execution_state="idle"),
            ],
            shell_after_execute=[
                _kernel_message("execute_reply", status="ok", execution_count=7)
            ],
        )
        kernel = _ExecutionKernel(client)

        result = execute_with_timeout(
            kernel, "pass", timeout=1, silent=True, store_history=False
        )

        assert result == {
            "status": "ok",
            "outputs": [
                {
                    "output_type": "display_data",
                    "data": {"text/plain": "result"},
                    "metadata": {},
                }
            ],
            "execution_count": 7,
        }
        assert client.execute_calls == [
            (
                "pass",
                {
                    "silent": True,
                    "store_history": False,
                    "user_expressions": None,
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
            )
        ]
        assert kernel.interrupt_calls == 0

    def test_timeout_carries_printed_output_but_unsent_request_does_not(self):
        from jupyter_jcli.kernel import ExecutionTimeout, execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[
                _kernel_message(
                    "stream", name="stdout", text="printed before timeout\\n"
                )
            ]
        )
        kernel = _ExecutionKernel(
            client,
            interrupt_messages=[_kernel_message("status", execution_state="idle")],
        )
        with pytest.raises(ExecutionTimeout) as caught:
            execute_with_timeout(kernel, "print('hello'); hang()", timeout=0.01)
        assert caught.value.partial_result == {
            "execution_count": None,
            "outputs": [
                {
                    "output_type": "stream",
                    "name": "stdout",
                    "text": "printed before timeout\\n",
                }
            ],
            "status": "error",
        }
        with pytest.raises(ExecutionTimeout) as unsent:
            execute_with_timeout(kernel, "pass", timeout=0)
        assert unsent.value.partial_result is None

    @pytest.mark.parametrize("silent", [False, True])
    def test_deadline_interrupts_and_reports_timeout(self, silent):
        from jupyter_jcli.kernel import ExecutionTimeout, execute_with_timeout

        client = _ExecutionClient()
        kernel = _ExecutionKernel(
            client,
            interrupt_messages=[_kernel_message("status", execution_state="idle")],
        )

        with pytest.raises(
            ExecutionTimeout, match="interrupted and returned to idle"
        ) as caught:
            execute_with_timeout(kernel, "long_running()", timeout=0.01, silent=silent)

        assert (caught.value.partial_result is None) == silent
        assert kernel.interrupt_calls == 1

    def test_interrupt_failure_is_distinct(self):
        from jupyter_jcli.kernel import KernelInterruptFailed, execute_with_timeout

        client = _ExecutionClient()
        kernel = _ExecutionKernel(client, interrupt_error=OSError("server unavailable"))

        with pytest.raises(KernelInterruptFailed, match="server unavailable"):
            execute_with_timeout(kernel, "long_running()", timeout=0.01)

        assert kernel.interrupt_calls == 1

    def test_missing_idle_after_interrupt_has_a_hard_deadline(self):
        from jupyter_jcli.kernel import KernelInterruptFailed, execute_with_timeout

        client = _ExecutionClient()
        kernel = _ExecutionKernel(client)
        started = time.monotonic()

        with (
            patch("jupyter_jcli.kernel._KERNEL_INTERRUPT_RECOVERY_TIMEOUT", 0.01),
            pytest.raises(KernelInterruptFailed, match="did not return to idle"),
        ):
            execute_with_timeout(kernel, "long_running()", timeout=0.01)

        assert time.monotonic() - started < 0.2
        assert kernel.interrupt_calls == 1

    def test_iopub_flush_cannot_consume_the_execution_deadline(self):
        from jupyter_jcli.kernel import ExecutionTimeout, execute_with_timeout

        client = _ExecutionClient()
        client.iopub_channel = _ContinuouslyReadyChannel()
        kernel = _ExecutionKernel(client)

        with pytest.raises(ExecutionTimeout, match="before the request was sent"):
            execute_with_timeout(kernel, "pass", timeout=0.01)

        assert client.execute_calls == []
        assert kernel.interrupt_calls == 0

    def test_output_observer_tracks_clear_wait_and_display_updates(self):
        from copy import deepcopy

        from jupyter_jcli.kernel import execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[
                _kernel_message(
                    "display_data",
                    data={"text/plain": "old"},
                    metadata={},
                    transient={"display_id": "display-1"},
                ),
                _kernel_message(
                    "update_display_data",
                    data={"text/plain": "new"},
                    metadata={},
                    transient={"display_id": "display-1"},
                ),
                _kernel_message("clear_output", wait=True),
                _kernel_message("stream", name="stdout", text="after clear\\n"),
                _kernel_message("status", execution_state="idle"),
            ],
            shell_after_execute=[
                _kernel_message("execute_reply", status="ok", execution_count=8)
            ],
        )
        observations = []

        def observe(message, outputs, changed, execution_count):
            observations.append(
                (
                    message,
                    deepcopy(outputs),
                    changed,
                    execution_count,
                )
            )

        result = execute_with_timeout(
            _ExecutionKernel(client), "display()", timeout=1, observer=observe
        )

        update = next(
            item
            for item in observations
            if item[0] and item[0]["header"]["msg_type"] == "update_display_data"
        )
        assert update[1][0]["data"] == {"text/plain": "new"}
        deferred_clear = next(
            item
            for item in observations
            if item[0]
            and item[0]["header"]["msg_type"] == "clear_output"
            and item[0]["content"].get("wait") is True
        )
        assert len(deferred_clear[1]) == 1
        applied_clear = next(
            item
            for item in observations
            if item[0]
            and item[0]["header"]["msg_type"] == "clear_output"
            and item[0]["content"].get("wait") is False
        )
        assert applied_clear[1] == []
        assert result["execution_count"] == 8
        assert result["outputs"] == [
            {"output_type": "stream", "name": "stdout", "text": "after clear\\n"}
        ]

    def test_wait_clear_does_not_erase_display_update(self):
        from jupyter_jcli.kernel import execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[
                _kernel_message(
                    "display_data",
                    data={"text/plain": "old"},
                    metadata={},
                    transient={"display_id": "d"},
                ),
                _kernel_message("clear_output", wait=True),
                _kernel_message(
                    "update_display_data",
                    data={"text/plain": "new"},
                    metadata={},
                    transient={"display_id": "d"},
                ),
                _kernel_message("status", execution_state="idle"),
            ],
            shell_after_execute=[_kernel_message("execute_reply", status="ok")],
        )
        result = execute_with_timeout(_ExecutionKernel(client), "pass", timeout=1)
        assert result["outputs"][0]["data"]["text/plain"] == "new"

    def test_observer_failure_drains_execution_before_raising(self):
        from jupyter_jcli.kernel import ExecutionObserverError, execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[
                _kernel_message("stream", name="stdout", text="before failure\\n"),
                _kernel_message("status", execution_state="idle"),
            ],
            shell_after_execute=[
                _kernel_message("execute_reply", status="ok", execution_count=9)
            ],
        )
        kernel = _ExecutionKernel(client)

        def fail_observer(*_args):
            raise OSError("checkpoint failed")

        with pytest.raises(ExecutionObserverError, match="checkpoint failed") as caught:
            execute_with_timeout(
                kernel, "print('x')", timeout=1, observer=fail_observer
            )

        assert caught.value.result["status"] == "error"
        assert caught.value.result["execution_count"] == 9
        assert caught.value.kernel_error is None
        assert kernel.interrupt_calls == 1
        assert client.iopub_channel.messages.empty()

    def test_observer_interrupt_failure_keeps_original_error(self):
        from jupyter_jcli.kernel import (
            ExecutionObserverError,
            KernelInterruptFailed,
            execute_with_timeout,
        )

        client = _ExecutionClient(
            iopub_after_execute=[_kernel_message("stream", name="stdout", text="x")]
        )
        kernel = _ExecutionKernel(client, interrupt_error=OSError("interrupt refused"))

        def fail(*_args):
            raise OSError("checkpoint failed")

        with pytest.raises(ExecutionObserverError, match="checkpoint failed") as caught:
            execute_with_timeout(kernel, "pass", timeout=1, observer=fail)
        assert isinstance(caught.value.kernel_error, KernelInterruptFailed)
        assert "interrupt refused" in str(caught.value)
        assert kernel.interrupt_calls == 1

    def test_silent_execution_does_not_notify_observer(self):
        from jupyter_jcli.kernel import execute_with_timeout

        client = _ExecutionClient(
            iopub_after_execute=[_kernel_message("status", execution_state="idle")],
            shell_after_execute=[
                _kernel_message("execute_reply", status="ok", execution_count=1)
            ],
        )
        observed = []
        result = execute_with_timeout(
            _ExecutionKernel(client),
            "setup()",
            timeout=1,
            silent=True,
            observer=lambda *args: observed.append(args),
        )

        assert result["status"] == "ok"
        assert observed == []


def test_file_timeout_marks_notebook_failure_and_unsent_keeps_outputs(tmp_path):
    from jupyter_jcli.file_execution import execute_file
    from jupyter_jcli.kernel import ExecutionTimeout

    script = tmp_path / "partial.py"
    script.write_text("# %%\nprint('output')\n")
    old_output = {"output_type": "stream", "name": "stdout", "text": "old\n"}
    partial_output = {"output_type": "stream", "name": "stdout", "text": "partial\n"}
    outcome = [
        {"status": "ok", "outputs": [old_output], "execution_count": 1},
        ExecutionTimeout(
            "interrupted",
            {"status": "error", "outputs": [partial_output], "execution_count": None},
        ),
        ExecutionTimeout("before the request was sent"),
    ]

    def fake_execute(*args, **kwargs):
        result = outcome.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with (
        patch("jupyter_jcli.kernel.kernel_connection", return_value=nullcontext(None)),
        patch(
            "jupyter_jcli.kernel.expression_display_mode", return_value=nullcontext()
        ),
        patch("jupyter_jcli.kernel.execute_with_timeout", side_effect=fake_execute),
    ):

        def execute():
            return execute_file(
                "url", None, "kernel", str(script), "0", "last_expr", None
            )

        execute()
        notebook = script.with_suffix(".ipynb")
        assert "old" in str(json.loads(notebook.read_text())["cells"][0]["outputs"])
        with pytest.raises(ExecutionTimeout, match="interrupted"):
            execute()
        outputs = json.loads(notebook.read_text())["cells"][0]["outputs"]
        assert "".join(outputs[0]["text"]) == "partial\n"
        assert outputs[-1] == {
            "output_type": "error",
            "ename": "ExecutionTimeout",
            "evalue": "interrupted",
            "traceback": [],
        }
        with pytest.raises(ExecutionTimeout, match="before the request was sent"):
            execute()
        assert json.loads(notebook.read_text())["cells"][0]["outputs"] == outputs


# ---------------------------------------------------------------------------
# Integration tests - real Jupyter server
# ---------------------------------------------------------------------------


class TestSigintHandlerUnit:
    """Unit tests for the SIGINT handler created by _make_interrupt_handler."""

    def test_handler_posts_to_interrupt_api(self):
        """Handler calls the kernel interrupt HTTP API."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen") as mock_urlopen,
            patch("jupyter_jcli.kernel.signal.signal"),
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

    def test_handler_restores_sigint_default_before_interrupt_request(self):
        """The handler's first action is SIG_DFL, already installed at urlopen time."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        original = signal.getsignal(signal.SIGINT)
        handler_at_urlopen = {}

        def record_urlopen(*_args, **_kwargs):
            handler_at_urlopen["value"] = signal.getsignal(signal.SIGINT)
            return MagicMock()

        try:
            with (
                patch(
                    "jupyter_jcli.kernel.urllib.request.urlopen",
                    side_effect=record_urlopen,
                ),
                patch("jupyter_jcli.kernel.sys.exit") as mock_exit,
            ):
                handler = _make_interrupt_handler("http://srv:8888", "tok", "kid-1")
                handler(signal.SIGINT, None)
        finally:
            signal.signal(signal.SIGINT, original)

        assert handler_at_urlopen["value"] is signal.SIG_DFL
        assert mock_exit.call_args_list == [call(128 + signal.SIGINT)]

    def test_handler_no_token(self):
        """Handler works without a token (no Authorization header)."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch("jupyter_jcli.kernel.urllib.request.urlopen") as mock_urlopen,
            patch("jupyter_jcli.kernel.signal.signal"),
            patch("jupyter_jcli.kernel.sys.exit"),
        ):
            handler = _make_interrupt_handler("http://srv:8888", None, "kid-1")
            handler(signal.SIGINT, None)

            req = mock_urlopen.call_args[0][0]
            assert req.get_header("Authorization") is None

    def test_handler_http_error_is_silent(self, capsys):
        """HTTP errors are silently ignored — exit still happens with no output."""
        from jupyter_jcli.kernel import _make_interrupt_handler

        with (
            patch(
                "jupyter_jcli.kernel.urllib.request.urlopen",
                side_effect=OSError("connection refused"),
            ),
            patch("jupyter_jcli.kernel.signal.signal"),
        ):
            handler = _make_interrupt_handler("http://srv:8888", "tok", "kid-1")
            with pytest.raises(SystemExit) as caught:
                handler(signal.SIGINT, None)

        captured = capsys.readouterr()
        assert caught.value.code == 128 + signal.SIGINT
        assert captured.out == ""
        assert captured.err == ""

    def test_signal_handler_set_and_restored(self):
        """kernel_connection covers the ready probe and restores handlers."""
        from jupyter_jcli.kernel import kernel_connection

        with (
            patch("jupyter_jcli.kernel.KernelClient"),
            patch("jupyter_jcli.kernel.signal.signal") as mock_signal_fn,
            patch("jupyter_jcli.kernel._wait_for_kernel_websocket_ready") as mock_ready,
        ):
            mock_signal_fn.return_value = "OLD_HANDLER"

            def assert_handlers_installed(*_args, **_kwargs):
                assert mock_signal_fn.call_count == 2
                assert mock_signal_fn.call_args_list[0].args[0] == signal.SIGINT
                assert mock_signal_fn.call_args_list[1].args[0] == signal.SIGTERM

            mock_ready.side_effect = assert_handlers_installed
            with kernel_connection("http://x", "tok", "kid"):
                pass

            # Should have been called twice for setup (SIGINT, SIGTERM)
            # and twice for teardown
            setup_calls = [
                c for c in mock_signal_fn.call_args_list if c[0][0] == signal.SIGINT
            ]
            assert len(setup_calls) == 2  # one set, one restore
            assert (
                len(
                    [
                        c
                        for c in mock_signal_fn.call_args_list
                        if c[0][0] == signal.SIGTERM
                    ]
                )
                == 2
            )
