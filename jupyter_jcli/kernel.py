"""Kernel execution via jupyter-kernel-client (WebSocket)."""

import queue
import signal
import socket
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager, suppress
from uuid import uuid4

from jupyter_kernel_client import KernelClient
from jupyter_kernel_client.client import output_hook
from jupyter_kernel_client.wsclient import KernelWebSocketClient

_KERNEL_READY_TIMEOUT = 30
# A failed attach attempt should be abandoned quickly.  The total budget below
# is still the user-visible connection timeout; this per-attempt budget prevents
# one unlucky WebSocket from consuming the entire exec attempt.
_KERNEL_READY_ATTEMPT_TIMEOUT = 2
_KERNEL_READY_MAX_ATTEMPTS = 3
_KERNEL_MESSAGE_POLL_INTERVAL = 0.1
_KERNEL_INTERRUPT_RECOVERY_TIMEOUT = 5
_WEBSOCKET_DISPATCH_TIMEOUT = 2
_WEBSOCKET_THREAD_JOIN_TIMEOUT = 3


class _JCLIKernelWebSocketClient(KernelWebSocketClient):
    """Keep websocket-client's dispatcher responsive to cross-thread close()."""

    def _run_websocket(self) -> None:
        kernel_socket = self.kernel_socket
        if kernel_socket is None:
            self.log.error("No websocket defined.")
            return
        try:
            self.log.debug("kernel socket: %s", kernel_socket.url)
            kernel_socket.run_forever(
                ping_interval=self.ping_interval,
                ping_timeout=_WEBSOCKET_DISPATCH_TIMEOUT,
                reconnect=self.reconnect_interval,
            )
        except ValueError as exc:
            self.log.error(
                "Unable to open websocket connection with %s",
                kernel_socket.url,
                exc_info=exc,
            )
        except BaseException as exc:
            self.log.error("Websocket listener thread stopped.", exc_info=exc)

    def stop_channels(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True

        kernel_socket = self.kernel_socket
        self.kernel_socket = None
        if kernel_socket is not None:
            kernel_socket.keep_running = False
            websocket = kernel_socket.sock
            raw_socket = websocket.sock if websocket is not None else None
            if raw_socket is not None:
                with suppress(OSError):
                    raw_socket.shutdown(socket.SHUT_RDWR)
            if websocket is not None:
                # WebSocket.close() reads the close reply while run_forever()
                # may already be reading it. Close the transport without that
                # competing read, then let run_forever() call on_close.
                with suppress(OSError):
                    websocket.shutdown()
                kernel_socket.sock = None

        connection_thread = self.connection_thread
        if connection_thread is not None and connection_thread.is_alive():
            connection_thread.join(_WEBSOCKET_THREAD_JOIN_TIMEOUT)
            if connection_thread.is_alive():
                self.log.warning("Failed to stop websocket connection thread.")
        self.connection_thread = None
        self.connection_ready.clear()


class ExecutionTimeout(TimeoutError):
    """Raised after a timed-out execution has returned to idle."""


class KernelInterruptFailed(RuntimeError):
    """Raised when a timed-out execution could not be interrupted."""


def _make_interrupt_handler(server_url: str, token: str | None, kernel_id: str):
    """Return a SIGINT/SIGTERM handler that interrupts the remote kernel.

    Design note: we assume there are no human users staring at a terminal.
    (Statistically, this is almost certainly correct.)  Therefore there is
    no double-confirm flow — the first signal immediately interrupts the
    kernel and exits.  Claude Code sends SIGINT on Esc and waits ~10 s
    before escalating to SIGKILL, so we have a window for cleanup.
    """

    def handler(signum: int, _frame) -> None:
        # Second signal → instant death (don't wait for the HTTP round-trip)
        signal.signal(signum, signal.SIG_DFL)
        with suppress(OSError):
            headers: dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            base = server_url.rstrip("/")
            req = urllib.request.Request(
                f"{base}/api/kernels/{kernel_id}/interrupt",
                data=b"",
                headers=headers,
            )
            urllib.request.urlopen(req, timeout=2).close()
        sys.exit(128 + signum)

    return handler


def _format_timeout(timeout: float | None) -> str:
    if timeout is None:
        return "without timing out"
    if float(timeout).is_integer():
        return f"in {int(timeout)} seconds"
    return f"in {timeout:g} seconds"


def _wait_for_kernel_websocket_ready(
    kernel: KernelClient, timeout: float | None = 30
) -> None:
    """Wait until this WebSocket can round-trip shell and IOPub messages.

    Jupyter Server finishes a server-side nudge before subscribing ZMQ
    channels to the WebSocket.  If execute_request is sent before that
    subscription is installed, the kernel can reply on ZMQ with no forwarding
    callback and the client will hang.  A matching kernel_info_reply proves the
    shell path, while its matching IOPub idle status proves the path that every
    subsequent execution needs in order to complete.
    """
    client = kernel._manager.client
    deadline = None if timeout is None else time.monotonic() + timeout
    sent_msg_ids: set[str] = set()
    shell_replies: dict[str, dict] = {}
    iopub_idle_msg_ids: set[str] = set()
    next_probe_at = 0.0

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise TimeoutError(f"Kernel didn't respond {_format_timeout(timeout)}")

        if now >= next_probe_at:
            # Send a harmless request over the same WebSocket that will later
            # carry execute_request.  A matching shell reply proves that this
            # specific attach has server-side outgoing forwarding installed.
            # Fixed sleeps cannot prove that property because the subscription
            # is created after the WebSocket opens.
            sent_msg_ids.add(client.kernel_info())
            next_probe_at = now + 0.5

        wait_timeout = 0.2
        if deadline is not None:
            wait_timeout = min(wait_timeout, max(0.0, deadline - time.monotonic()))

        try:
            shell_msg = client.shell_channel.get_msg(timeout=wait_timeout)
        except (queue.Empty, TimeoutError):
            shell_msg = None

        if shell_msg is not None and shell_msg.get("msg_type") == "kernel_info_reply":
            parent_id = shell_msg.get("parent_header", {}).get("msg_id")
            if parent_id in sent_msg_ids:
                shell_replies[parent_id] = shell_msg

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Kernel didn't respond {_format_timeout(timeout)}")
            try:
                iopub_msg = client.iopub_channel.get_msg(timeout=0)
            except (queue.Empty, TimeoutError):
                break
            msg_type = iopub_msg.get("msg_type") or iopub_msg.get("header", {}).get(
                "msg_type"
            )
            parent_id = iopub_msg.get("parent_header", {}).get("msg_id")
            if (
                msg_type == "status"
                and parent_id in sent_msg_ids
                and iopub_msg.get("content", {}).get("execution_state") == "idle"
            ):
                iopub_idle_msg_ids.add(parent_id)

        ready_msg_ids = shell_replies.keys() & iopub_idle_msg_ids
        if ready_msg_ids:
            ready_reply = shell_replies[next(iter(ready_msg_ids))]
            handle_reply = getattr(client, "_handle_kernel_info_reply", None)
            if handle_reply is not None:
                handle_reply(ready_reply)
            return


def _start_ready_kernel_connection(
    server_url: str,
    token: str | None,
    kernel_id: str,
    timeout: float = _KERNEL_READY_TIMEOUT,
) -> KernelClient:
    """Start a KernelClient and retry fresh WebSockets if attach probing wedges.

    This retry is deliberately scoped to the pre-execute attach handshake.  At
    this point j-cli has only sent kernel_info_request probes, never user code,
    so closing a wedged WebSocket and creating a fresh one is safe.  Retrying
    after execute_request would not be safe because user code may already have
    run even if the reply was dropped.
    """
    deadline = time.monotonic() + timeout
    last_timeout: TimeoutError | None = None

    for attempt in range(_KERNEL_READY_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempt_timeout = min(_KERNEL_READY_ATTEMPT_TIMEOUT, remaining)

        kernel = KernelClient(
            server_url=server_url,
            token=token,
            kernel_id=kernel_id,
            client_factory=_JCLIKernelWebSocketClient,
            client_kwargs={"timeout": attempt_timeout},
        )
        try:
            # KernelClient.start() opens the WebSocket.  A successful open only
            # means the HTTP upgrade completed; it does not prove Jupyter
            # Server has finished nudge() and subscribed ZMQ outgoing streams.
            kernel.start(timeout=attempt_timeout)
            _wait_for_kernel_websocket_ready(kernel, timeout=attempt_timeout)
        except TimeoutError as exc:
            last_timeout = exc
            # Treat timeout as a failed attach attempt, not as a dead kernel.
            # stop() closes this WebSocket; it does not shut down the existing
            # kernel because this client did not create it.
            kernel.stop()
            if attempt + 1 >= _KERNEL_READY_MAX_ATTEMPTS:
                break
            continue
        except BaseException:
            kernel.stop()
            raise
        return kernel

    raise TimeoutError(
        f"Kernel didn't respond {_format_timeout(timeout)}"
    ) from last_timeout


@contextmanager
def kernel_connection(server_url: str, token: str | None, kernel_id: str):
    """Context manager that yields a started KernelClient."""
    is_main = threading.current_thread() is threading.main_thread()
    if is_main:
        handler = _make_interrupt_handler(server_url, token, kernel_id)
        old_sigint = signal.signal(signal.SIGINT, handler)
        old_sigterm = signal.signal(signal.SIGTERM, handler)
    try:
        kernel = _start_ready_kernel_connection(server_url, token, kernel_id)
        try:
            yield kernel
        finally:
            kernel.stop()
    finally:
        if is_main:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)


@contextmanager
def expression_display_mode(
    kernel: KernelClient, display_mode: str, timeout: float = 10
):
    """Temporarily configure how IPython displays top-level expressions."""
    state_attr = f"_jcli_ast_node_interactivity_{uuid4().hex}"
    setup_result = execute_with_timeout(
        kernel,
        "setattr(get_ipython(), "
        f"{state_attr!r}, get_ipython().ast_node_interactivity)\n"
        f"get_ipython().ast_node_interactivity = {display_mode!r}",
        timeout=timeout,
        silent=True,
        store_history=False,
    )
    if setup_result.get("status") != "ok":
        raise RuntimeError(
            f"Kernel failed to enable IPython display mode {display_mode!r}"
        )

    active_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        try:
            execute_with_timeout(
                kernel,
                "_jcli_shell = get_ipython()\n"
                f"_jcli_shell.ast_node_interactivity = getattr(_jcli_shell, {state_attr!r})\n"
                f"delattr(_jcli_shell, {state_attr!r})\n"
                "del _jcli_shell",
                timeout=10,
                silent=True,
                store_history=False,
            )
        except Exception:
            if active_error is None:
                raise


def execute_with_timeout(
    kernel: KernelClient,
    code: str,
    timeout: float,
    *,
    silent: bool = False,
    store_history: bool = True,
    user_expressions: dict | None = None,
    stop_on_error: bool = True,
) -> dict:
    """Execute code and interrupt the remote kernel when the deadline expires."""
    if timeout <= 0:
        raise ExecutionTimeout("Execution deadline expired before the request was sent")

    # jupyter-kernel-client 0.9.0's execute_interactive() recalculates its
    # timeout as zero at the deadline, then loops forever on Event.wait(0).
    # Drive its channel queues ourselves so a lost WebSocket cannot defeat the
    # caller's deadline. Remove this workaround once upstream raises on expiry.
    client = kernel._manager.client
    deadline = time.monotonic() + timeout
    outputs: list[dict] = []
    reply: dict | None = None
    idle_seen = False
    timed_out = False
    recovery_deadline: float | None = None

    while time.monotonic() < deadline:
        try:
            client.iopub_channel.get_msg(timeout=0)
        except (queue.Empty, TimeoutError):
            break
    else:
        raise ExecutionTimeout("Execution deadline expired before the request was sent")

    msg_id = client.execute(
        code,
        silent=silent,
        store_history=store_history,
        user_expressions=user_expressions,
        allow_stdin=False,
        stop_on_error=stop_on_error,
    )

    while True:
        now = time.monotonic()
        active_deadline = recovery_deadline if timed_out else deadline
        assert active_deadline is not None
        remaining = active_deadline - now

        if remaining <= 0:
            if timed_out:
                raise KernelInterruptFailed(
                    "Execution deadline expired; the kernel did not return to idle "
                    f"within {_KERNEL_INTERRUPT_RECOVERY_TIMEOUT:g} seconds"
                )
            if idle_seen:
                raise ExecutionTimeout(
                    "Execution deadline expired while waiting for the execute reply; "
                    "the kernel returned to idle"
                )
            try:
                kernel.interrupt(timeout=2)
            except Exception as exc:
                raise KernelInterruptFailed(
                    f"Execution deadline expired and the kernel interrupt failed: {exc}"
                ) from exc
            timed_out = True
            recovery_deadline = time.monotonic() + _KERNEL_INTERRUPT_RECOVERY_TIMEOUT
            continue

        wait_timeout = min(_KERNEL_MESSAGE_POLL_INTERVAL, remaining)
        try:
            msg = client.iopub_channel.get_msg(timeout=wait_timeout)
        except (queue.Empty, TimeoutError):
            msg = None

        if msg is not None and msg.get("parent_header", {}).get("msg_id") == msg_id:
            output_hook(outputs, msg)
            if (
                msg.get("header", {}).get("msg_type") == "status"
                and msg.get("content", {}).get("execution_state") == "idle"
            ):
                idle_seen = True

        try:
            shell_msg = client.shell_channel.get_msg(timeout=0)
        except (queue.Empty, TimeoutError):
            shell_msg = None
        if (
            shell_msg is not None
            and shell_msg.get("parent_header", {}).get("msg_id") == msg_id
        ):
            reply = shell_msg

        if timed_out and idle_seen:
            raise ExecutionTimeout(
                "Execution deadline expired; the kernel was interrupted and returned to idle"
            )
        if not timed_out and idle_seen and reply is not None:
            for output in outputs:
                output.pop("transient", None)
            content = reply["content"]
            return {
                "execution_count": content.get("execution_count"),
                "outputs": outputs,
                "status": content["status"],
            }


def execute_code(
    server_url: str,
    token: str | None,
    kernel_id: str,
    code: str,
    timeout: int = 300,
    display_mode: str = "last_expr",
) -> dict:
    """Execute code in a kernel and return raw result.

    Returns dict with 'outputs' key containing list of output dicts,
    and 'execution_count'.
    """
    with (
        kernel_connection(server_url, token, kernel_id) as kernel,
        expression_display_mode(kernel, display_mode, timeout=10),
    ):
        deadline = time.monotonic() + timeout
        remaining = max(deadline - time.monotonic(), 0)
        return execute_with_timeout(kernel, code, timeout=remaining)
