"""Kernel execution via jupyter-kernel-client (WebSocket)."""

import signal
import sys
import threading
import urllib.request
from contextlib import contextmanager

from jupyter_kernel_client import KernelClient


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


@contextmanager
def kernel_connection(server_url: str, token: str | None, kernel_id: str):
    """Context manager that yields a started KernelClient."""
    kernel = KernelClient(
        server_url=server_url,
        token=token,
        kernel_id=kernel_id,
    )
    kernel.start()
    try:
        # Ensure server-side ZMQ→WebSocket forwarding pipeline is ready before
        # yielding, to prevent the nudge() race condition where execute_reply is
        # dropped because the forwarding callback (subscribe) hasn't been set up yet.
        kernel._manager.client.wait_for_ready(timeout=30)
    except BaseException:
        kernel.stop()
        raise

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


def execute_code(
    server_url: str,
    token: str | None,
    kernel_id: str,
    code: str,
    timeout: int = 300,
) -> dict:
    """Execute code in a kernel and return raw result.

    Returns dict with 'outputs' key containing list of output dicts,
    and 'execution_count'.
    """
    with kernel_connection(server_url, token, kernel_id) as kernel:
        return kernel.execute(code, timeout=timeout)
