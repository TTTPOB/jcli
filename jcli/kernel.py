"""Kernel execution via jupyter-kernel-client (WebSocket)."""

from jupyter_kernel_client import KernelClient


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
    kernel = KernelClient(
        server_url=server_url,
        token=token,
        kernel_id=kernel_id,
    )
    kernel.start()
    try:
        result = kernel.execute(code)
        return result
    finally:
        kernel.stop()
