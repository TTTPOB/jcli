"""Connection and launch-command regression cases."""

import json

import pytest
from click.testing import CliRunner
from jupyter_server_client.exceptions import (
    AuthenticationError,
    ForbiddenError,
    JupyterConnectionError,
    JupyterTimeoutError,
    NotFoundError,
)

from jupyter_jcli.cli import main
from jupyter_jcli.commands._server_errors import server_error_code
from jupyter_jcli.server import ServerClient


def _error(args, monkeypatch):
    monkeypatch.delenv("JCLI_JUPYTER_SERVER_TOKEN", raising=False)
    result = CliRunner().invoke(main, ["--json", *args])
    assert result.exit_code == 1
    return json.loads(result.stderr), result


def test_serve_requires_exported_nonempty_token_even_with_cli_token(monkeypatch):
    error, result = _error(
        ["-t", "private-token", "serve-cmd", "--serve-backend", "lab"],
        monkeypatch,
    )
    assert error["code"] == "SERVE_CMD_NO_TOKEN"
    assert "private-token" not in result.output
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "")
    result = CliRunner().invoke(main, ["--json", "serve-cmd", "--serve-backend", "lab"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "SERVE_CMD_NO_TOKEN"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:invalid",
        "http://localhost:65536",
        "http://localhost:0",
        "http://[broken",
    ],
)
def test_serve_invalid_url_is_structured_error(monkeypatch, url):
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "valid")
    result = CliRunner().invoke(
        main, ["-s", url, "--json", "serve-cmd", "--serve-backend", "lab"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "SERVE_CMD_BAD_URL"


@pytest.mark.parametrize("port", ["-1", "0", "65536"])
def test_serve_rejects_invalid_override_port(monkeypatch, port):
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "valid")
    result = CliRunner().invoke(
        main, ["--json", "serve-cmd", "--serve-backend", "lab", "--port", port]
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "SERVE_CMD_BAD_URL"


def test_serve_disables_port_retry(monkeypatch):
    monkeypatch.setenv("JCLI_JUPYTER_SERVER_TOKEN", "valid")
    result = CliRunner().invoke(main, ["--json", "serve-cmd", "--serve-backend", "lab"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "--ServerApp.port_retries=0" in data["command"]
    assert "--ServerApp.port_retries=0" in data["argv_template"]


def test_server_uses_default_tls_verification():
    client = ServerClient("https://localhost:8888")
    assert client._client.http_client.session.verify is True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("missing"), "SESSION_NOT_FOUND"),
        (AuthenticationError("unauthorized"), "AUTH_FAILED"),
        (ForbiddenError("forbidden"), "AUTH_FAILED"),
        (JupyterTimeoutError("slow"), "TIMEOUT"),
        (JupyterConnectionError("offline"), "CONNECTION_FAILED"),
        (ValueError("bad response"), "CONNECTION_FAILED"),
    ],
)
def test_classify_actual_rest_client_exceptions(error, expected):
    assert server_error_code(error, "SESSION_NOT_FOUND") == expected


@pytest.mark.parametrize("command", ["exec", "kernel", "session"])
def test_selector_timeout_is_not_reported_missing(monkeypatch, command):
    def fail(*_args):
        raise JupyterTimeoutError("slow")

    monkeypatch.setattr("jupyter_jcli.server.ServerClient.list_sessions", fail)
    args = {
        "exec": ["exec", "missing", "--code", "1"],
        "kernel": ["kernel", "interrupt", "missing"],
        "session": ["session", "kill", "missing"],
    }[command]
    error, _ = _error(args, monkeypatch)
    assert error["code"] == "TIMEOUT"


def test_kernel_operation_auth_failure(monkeypatch):
    monkeypatch.setattr(
        "jupyter_jcli.server.ServerClient.resolve_kernel",
        lambda _self, _selector: ("session-id", "kernel-id"),
    )
    monkeypatch.setattr(
        "jupyter_jcli.server.ServerClient.get_session_selector",
        lambda _self, _session_id: "session-id",
    )

    def fail(*_args):
        raise AuthenticationError("unauthorized")

    monkeypatch.setattr("jupyter_jcli.server.ServerClient.interrupt_kernel", fail)
    error, _ = _error(["kernel", "interrupt", "session-id"], monkeypatch)
    assert error["code"] == "AUTH_FAILED"
