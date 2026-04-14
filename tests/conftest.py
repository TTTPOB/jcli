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


@pytest.fixture(scope="module")
def live_session(jupyter_server):
    """A kernel session shared across one test module.

    Scoped to module (not session) so each test file gets a fresh kernel
    process.  This prevents accumulated state or a stale WebSocket from one
    module affecting the next, which would otherwise cause
    execute_interactive to spin forever when the kernel is unresponsive.

    Tests that only run code and inspect results should use this fixture
    instead of creating their own session — kernel startup is expensive.
    Tests that mutate kernel lifecycle (restart, interrupt) must create
    their own private session via _create_session / _kill_session.
    """
    import json
    from click.testing import CliRunner
    from jupyter_jcli.cli import main

    runner = CliRunner()
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "session", "create", "--kernel", "python3",
    ])
    data = json.loads(result.output)
    sid = data["session_id"]
    yield {**jupyter_server, "session_id": sid}
    runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "session", "kill", sid,
    ])


@pytest.fixture(scope="module")
def live_kernel(live_session):
    """A persistent WebSocket connection to the module's kernel.

    Opened once per test module and reused across all tests in that module.
    Tests that want to execute code or inspect variables should use
    mock_kernel_connection or mock_execute_code so the CLI path reuses this
    connection instead of opening a new one for every call.
    """
    from jupyter_jcli.kernel import kernel_connection
    from jupyter_jcli.server import get_kernel_id_for_session

    kernel_id = get_kernel_id_for_session(
        live_session["url"], live_session["session_id"], live_session["token"]
    )
    with kernel_connection(live_session["url"], live_session["token"], kernel_id) as kernel:
        yield kernel


@pytest.fixture
def mock_kernel_connection(live_kernel):
    """Patch kernel_connection so CLI commands reuse live_kernel.

    Use this for tests that invoke exec --file or vars through the CLI.
    The fixture patches the canonical source (jupyter_jcli.kernel) which
    is where both exec_cmd and vars_cmd lazily import from.
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _reuse(*args, **kwargs):
        yield live_kernel

    with patch("jupyter_jcli.kernel.kernel_connection", _reuse):
        yield live_kernel


@pytest.fixture
def mock_execute_code(live_kernel):
    """Patch execute_code so exec --code reuses live_kernel.

    Use this for tests that invoke exec --code through the CLI.
    """
    from unittest.mock import patch

    def _reuse(url, token, kid, code, timeout=300):
        return live_kernel.execute(code, timeout=timeout)

    with patch("jupyter_jcli.kernel.execute_code", side_effect=_reuse):
        yield live_kernel
