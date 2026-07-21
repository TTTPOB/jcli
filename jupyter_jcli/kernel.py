"""Kernel execution via jupyter-kernel-client (WebSocket)."""

import queue
import signal
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from uuid import uuid4

from jupyter_kernel_client import KernelClient


_KERNEL_READY_TIMEOUT = 30
# A failed attach attempt should be abandoned quickly.  The total budget below
# is still the user-visible connection timeout; this per-attempt budget prevents
# one unlucky WebSocket from consuming the entire exec attempt.
_KERNEL_READY_ATTEMPT_TIMEOUT = 10
_KERNEL_READY_MAX_ATTEMPTS = 3


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
        try:
            headers: dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            base = server_url.rstrip("/")
            req = urllib.request.Request(
                f"{base}/api/kernels/{kernel_id}/interrupt",
                data=b"",
                headers=headers,
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass
        sys.exit(128 + signum)

    return handler


def _format_timeout(timeout: float | None) -> str:
    if timeout is None:
        return "without timing out"
    if float(timeout).is_integer():
        return f"in {int(timeout)} seconds"
    return f"in {timeout:g} seconds"


def _drain_iopub(client) -> None:
    """Best-effort drain of startup/status messages left by the ready probe.

    The probe only proves shell forwarding.  IOPub startup/status messages may
    already be queued by then, and leaving them in the queue can confuse the
    first real execute_interactive() call because it flushes pre-existing IOPub
    messages.  Draining here is intentionally best-effort: IOPub is not part of
    the readiness proof, so absence of an IOPub message must not fail attach.
    """
    while True:
        try:
            client.iopub_channel.get_msg(timeout=0)
        except (queue.Empty, TimeoutError):
            return


def _wait_for_kernel_websocket_ready(
    kernel: KernelClient, timeout: float | None = 30
) -> None:
    """Wait until this WebSocket can round-trip shell messages.

    Jupyter Server finishes a server-side nudge before subscribing ZMQ
    channels to the WebSocket.  If execute_request is sent before that
    subscription is installed, the kernel can reply on ZMQ with no forwarding
    callback and the client will hang.  The upstream wait_for_ready() also
    waits for an IOPub message, which can false-timeout on fresh WebSockets.
    For j-cli, the required invariant is a successful shell round-trip on this
    WebSocket; once shell replies are forwarded, subsequent execute requests
    are safe to send.
    """
    client = kernel._manager.client
    deadline = None if timeout is None else time.monotonic() + timeout
    sent_msg_ids: set[str] = set()
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
            msg = client.shell_channel.get_msg(timeout=wait_timeout)
        except (queue.Empty, TimeoutError):
            continue

        # Ignore old shell traffic from previous requests.  The safety proof is
        # tied to a reply for one of this probe's msg_ids on this WebSocket.
        if msg.get("msg_type") != "kernel_info_reply":
            continue
        if msg.get("parent_header", {}).get("msg_id") not in sent_msg_ids:
            continue

        handle_reply = getattr(client, "_handle_kernel_info_reply", None)
        if handle_reply is not None:
            handle_reply(msg)
        _drain_iopub(client)
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
    kernel = _start_ready_kernel_connection(server_url, token, kernel_id)

    is_main = threading.current_thread() is threading.main_thread()
    if is_main:
        handler = _make_interrupt_handler(server_url, token, kernel_id)
        old_sigint = signal.signal(signal.SIGINT, handler)
        old_sigterm = signal.signal(signal.SIGTERM, handler)
    try:
        yield kernel
    finally:
        if is_main:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)
        kernel.stop()


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
    **execute_kwargs,
) -> dict:
    """Execute code and interrupt the remote kernel when the deadline expires."""
    if timeout <= 0:
        raise ExecutionTimeout("Execution deadline expired before the request was sent")

    finished = threading.Event()
    timed_out = threading.Event()
    interrupt_errors: list[Exception] = []

    def interrupt_at_deadline() -> None:
        if finished.wait(timeout):
            return
        timed_out.set()
        try:
            kernel.interrupt(timeout=2)
        except Exception as exc:
            interrupt_errors.append(exc)

    watchdog = threading.Thread(
        target=interrupt_at_deadline,
        name="jcli-execution-timeout",
        daemon=True,
    )
    watchdog.start()
    result: dict | None = None
    execution_error: Exception | None = None
    try:
        # jupyter-kernel-client 0.9.0 does not enforce its WebSocket timeout.
        # The watchdog owns the deadline and interrupt, while this call consumes
        # messages through the matching idle status after an interrupt.
        result = kernel.execute(code, timeout=None, **execute_kwargs)
    except Exception as exc:
        execution_error = exc
    finally:
        finished.set()
        watchdog.join()

    if timed_out.is_set():
        if interrupt_errors:
            raise KernelInterruptFailed(
                "Execution deadline expired and the kernel interrupt failed: "
                f"{interrupt_errors[0]}"
            ) from interrupt_errors[0]
        raise ExecutionTimeout(
            "Execution deadline expired; the kernel was interrupted and returned to idle"
        )
    if execution_error is not None:
        raise execution_error
    assert result is not None
    return result


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
    with kernel_connection(server_url, token, kernel_id) as kernel:
        with expression_display_mode(kernel, display_mode, timeout=10):
            deadline = time.monotonic() + timeout
            remaining = max(deadline - time.monotonic(), 0)
            return execute_with_timeout(kernel, code, timeout=remaining)
