"""Shared test fixtures: a real jupyter-server instance."""

import os
import signal
import socket
import subprocess
import sys
import time
import shutil

import pytest


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, token: str, timeout: float = 30) -> None:
    """Poll server until it responds or timeout, bypassing proxy."""
    import http.client
    from urllib.parse import urlparse

    parsed = urlparse(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            conn.request("GET", "/api/status", headers={"Authorization": f"token {token}"})
            resp = conn.getresponse()
            if resp.status == 200:
                conn.close()
                return
            conn.close()
        except (ConnectionError, OSError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Jupyter server at {url} did not start within {timeout}s")


@pytest.fixture(scope="session")
def jupyter_server():
    """Start a real jupyter-server for the test session.

    Yields a dict with 'url', 'token', and 'root_dir' keys.
    """
    port = _find_free_port()
    token = "test-token-jcli"
    url = f"http://127.0.0.1:{port}"

    base = "/tmp/jcli-test-server"
    # Clean slate
    if os.path.exists(base):
        shutil.rmtree(base)
    for d in ("root", "data", "runtime", "config"):
        os.makedirs(f"{base}/{d}", exist_ok=True)

    env = {
        **os.environ,
        "HOME": base,
        "JUPYTER_DATA_DIR": f"{base}/data",
        "JUPYTER_RUNTIME_DIR": f"{base}/runtime",
        "JUPYTER_CONFIG_DIR": f"{base}/config",
        "JUPYTER_PATH": "",
        "no_proxy": "127.0.0.1,localhost",
        "NO_PROXY": "127.0.0.1,localhost",
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jupyter_server",
            f"--port={port}",
            f"--IdentityProvider.token={token}",
            f"--ServerApp.root_dir={base}/root",
            "--ip=127.0.0.1",
            "--no-browser",
            "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        _wait_for_server(url, token)
        yield {"url": url, "token": token, "root_dir": f"{base}/root"}
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
