"""Tests for `j-cli serve-cmd`."""

import json

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.serve_cmd import ServeBackend


# ---------------------------------------------------------------------------
# ServeBackend enum behaviour
# ---------------------------------------------------------------------------

class TestServeBackendEnum:
    def test_members_exist(self):
        assert ServeBackend.LAB == "lab"
        assert ServeBackend.SERVER == "server"
        assert ServeBackend.NOTEBOOK == "notebook"

    def test_str_inheritance(self):
        assert isinstance(ServeBackend.LAB, str)

    def test_invalid_raises(self):
        import pytest
        with pytest.raises(ValueError):
            ServeBackend("bogus")


_DEFAULT_ENV = {
    "JCLI_JUPYTER_SERVER_URL": "http://localhost:8888",
    "JCLI_JUPYTER_SERVER_TOKEN": "test-token-abc",
}


def _invoke(runner: CliRunner, args: list[str], env: dict | None = None):
    return runner.invoke(
        main, ["serve-cmd"] + args, catch_exceptions=False, env=env or _DEFAULT_ENV,
    )


def _cmd_line(result) -> str:
    """Extract the jupyter ... command line from result.output (skips hint lines)."""
    for line in result.output.splitlines():
        if line.startswith("jupyter"):
            return line
    return result.output.strip()


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

class TestArgs:
    def test_missing_backend_errors(self):
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 2  # click missing required option

    def test_invalid_backend_errors(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "evil"])
        assert result.exit_code == 2  # click usage error
        combined = (result.output or "") + (result.stderr or "")
        assert "evil" in combined  # click names the bad value

    def test_lab_backend_ok(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab"])
        assert result.exit_code == 0
        assert "jupyter lab " in result.output

    def test_server_backend_ok(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "server"])
        assert result.exit_code == 0
        assert "jupyter server " in result.output

    def test_notebook_backend_ok(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "notebook"])
        assert result.exit_code == 0
        assert "jupyter notebook " in result.output


# ---------------------------------------------------------------------------
# Environment variable handling
# ---------------------------------------------------------------------------

class TestEnv:
    def test_missing_token_errors(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["serve-cmd", "--serve-backend", "lab"],
            env={"JCLI_JUPYTER_SERVER_URL": "http://localhost:8888"},
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        combined = (result.output or "") + (result.stderr or "")
        assert "SERVE_CMD_NO_TOKEN" in combined

    def test_url_parsed_into_ip_port(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["serve-cmd", "--serve-backend", "lab"],
            env={
                "JCLI_JUPYTER_SERVER_URL": "http://1.2.3.4:9999",
                "JCLI_JUPYTER_SERVER_TOKEN": "test-token",
            },
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "--ServerApp.ip=1.2.3.4" in result.output
        assert "--ServerApp.port=9999" in result.output

    def test_default_url_localhost_8888(self):
        """When URL env is unset, falls back to default http://localhost:8888."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["serve-cmd", "--serve-backend", "lab"],
            env={"JCLI_JUPYTER_SERVER_TOKEN": "test-token"},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "--ServerApp.ip=localhost" in result.output
        assert "--ServerApp.port=8888" in result.output


# ---------------------------------------------------------------------------
# CLI flag overrides
# ---------------------------------------------------------------------------

class TestOverrides:
    def test_ip_flag_overrides_env(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab", "--ip", "0.0.0.0"])
        assert result.exit_code == 0
        assert "--ServerApp.ip=0.0.0.0" in result.output

    def test_port_flag_overrides_env(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab", "--port", "10000"])
        assert result.exit_code == 0
        assert "--ServerApp.port=10000" in result.output

    def test_root_dir_is_quoted(self):
        """Paths with spaces are wrapped in single quotes by shlex.quote."""
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab", "--root-dir", "/tmp/my data"])
        assert result.exit_code == 0
        assert "'/tmp/my data'" in result.output


# ---------------------------------------------------------------------------
# Security: token is never inlined; hosts with metacharacters are rejected
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_token_never_inlined(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["serve-cmd", "--serve-backend", "lab"],
            env={
                "JCLI_JUPYTER_SERVER_URL": "http://localhost:8888",
                "JCLI_JUPYTER_SERVER_TOKEN": "SUPERSECRET",
            },
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "SUPERSECRET" not in result.output
        assert '"$JCLI_JUPYTER_SERVER_TOKEN"' in result.output
        # Exactly one reference in the command
        cmd = _cmd_line(result)
        assert cmd.count('"$JCLI_JUPYTER_SERVER_TOKEN"') == 1

    @pytest.mark.parametrize("bad_host", [
        "host;rm",
        "host`cmd`",
        "host$(evil)",
    ])
    def test_host_injection_rejected(self, bad_host: str):
        """Hosts containing shell metacharacters via --ip are rejected."""
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab", "--ip", bad_host])
        assert result.exit_code == 1
        combined = (result.output or "") + (result.stderr or "")
        assert "SERVE_CMD_BAD_URL" in combined

    def test_malformed_url_rejected(self):
        """A URL that yields no parseable hostname is rejected."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["serve-cmd", "--serve-backend", "lab"],
            env={
                "JCLI_JUPYTER_SERVER_URL": "http:",
                "JCLI_JUPYTER_SERVER_TOKEN": "test-token",
            },
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        combined = (result.output or "") + (result.stderr or "")
        assert "SERVE_CMD_BAD_URL" in combined


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------

class TestOutput:
    def test_no_browser_default_on(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab"])
        assert result.exit_code == 0
        assert "--no-browser" in result.output

    def test_browser_flag_drops_no_browser(self):
        runner = CliRunner()
        result = _invoke(runner, ["--serve-backend", "lab", "--browser"])
        assert result.exit_code == 0
        assert "--no-browser" not in result.output

    def test_hint_on_stderr(self):
        """Hint line (# ...) appears in human mode output; absent in JSON mode."""
        runner = CliRunner()
        # Human mode: hint is present in output (stderr mixed with stdout)
        result = _invoke(runner, ["--serve-backend", "lab"])
        assert result.exit_code == 0
        hint_lines = [ln for ln in result.output.splitlines() if ln.startswith("#")]
        assert hint_lines, "Expected at least one hint line starting with '#'"

        # JSON mode: hint must NOT appear (stdout is pure JSON)
        result_json = runner.invoke(
            main, ["--json", "serve-cmd", "--serve-backend", "lab"],
            env=_DEFAULT_ENV, catch_exceptions=False,
        )
        assert result_json.exit_code == 0
        assert "#" not in result_json.output or result_json.output.strip().startswith("{")
        # Must be valid JSON (no hint contamination)
        json.loads(result_json.output)


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------

class TestJson:
    def test_json_shape(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "serve-cmd", "--serve-backend", "lab"],
            env=_DEFAULT_ENV, catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "command" in data
        assert "argv_template" in data
        assert data["env_refs"] == ["JCLI_JUPYTER_SERVER_TOKEN"]

    def test_argv_template_is_list(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "serve-cmd", "--serve-backend", "server"],
            env=_DEFAULT_ENV, catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        argv = data["argv_template"]
        assert isinstance(argv, list)
        assert argv[0] == "jupyter"
        assert argv[1] == "server"
        assert all(isinstance(s, str) for s in argv)
