"""Kernel execution via jupyter-kernel-client (WebSocket)."""

from contextlib import contextmanager

from jupyter_kernel_client import KernelClient


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
        yield kernel
    finally:
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
