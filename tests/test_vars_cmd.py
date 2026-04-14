"""Test the 'vars' subcommand end-to-end against a live kernel."""

import json

import pytest

from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.vars_cmd import _emit_list
from jupyter_jcli.variables import VariableSource


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token, "--json",
        "session", "create", "--kernel", "python3",
    ])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _kill_session(runner, url, token, sid):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", sid])


class TestVarsCmdList:

    def test_json_output_shape(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        mock_kernel_connection.execute("_vc_x = 42; _vc_s = 'hi'", timeout=30)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "vars", live_session["session_id"],
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "variables" in data
        assert "source" in data
        assert data["source"] in (VariableSource.DAP.value, VariableSource.FALLBACK.value)

    def test_user_variables_in_output(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        mock_kernel_connection.execute("_vc_x = 42; _vc_s = 'hi'", timeout=30)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "vars", live_session["session_id"],
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        names = [v["name"] for v in data["variables"]]
        assert "_vc_x" in names
        assert "_vc_s" in names

        x_var = next(v for v in data["variables"] if v["name"] == "_vc_x")
        assert "42" in x_var["value"]

    def test_human_output_table(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        mock_kernel_connection.execute("_vc_my_var = 99", timeout=30)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "vars", live_session["session_id"],
        ])
        assert result.exit_code == 0, result.output
        assert "_vc_my_var" in result.output
        assert "99" in result.output


class TestVarsCmdSingleVar:

    def test_inspect_single_variable_json(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        mock_kernel_connection.execute("_vs_x = 42", timeout=30)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "vars", live_session["session_id"], "--name", "_vs_x",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["name"] == "_vs_x"
        assert "42" in data["value"]
        assert data["source"] in (VariableSource.DAP.value, VariableSource.FALLBACK.value)

    def test_inspect_missing_variable_exits_1(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "vars", live_session["session_id"], "--name", "__no_such_var__",
        ])
        assert result.exit_code == 1

    def test_rich_requires_name(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "--json", "vars", live_session["session_id"], "--rich",
        ])
        assert result.exit_code == 1


class TestVarsCmdDeadSession:

    def test_dead_session_exits_1(self, jupyter_server):
        runner = CliRunner()
        fake_sid = "00000000-0000-0000-0000-000000000000"
        result = runner.invoke(main, [
            "-s", jupyter_server["url"], "-t", jupyter_server["token"],
            "--json", "vars", fake_sid,
        ])
        assert result.exit_code == 1


class TestEmitListDefensiveness:
    """Unit tests — no live kernel; guard formatter against non-string values."""

    def test_emit_list_does_not_crash_on_list_value(self):
        from jupyter_jcli.cli import Context

        ctx = Context(server_url="http://localhost:8888", token=None, use_json=False)
        result = {
            "variables": [{"name": "lst", "type": "list", "value": [1, 2, 3]}],
            "source": VariableSource.FALLBACK,
        }
        _emit_list(ctx, result, "fake-session-id")

    def test_emit_list_output_contains_variable_name(self):
        import click as _click
        from jupyter_jcli.cli import Context

        ctx = Context(server_url="http://localhost:8888", token=None, use_json=False)
        result = {
            "variables": [{"name": "my_list", "type": "list", "value": [1, 2, 3]}],
            "source": VariableSource.FALLBACK,
        }
        with CliRunner().isolated_filesystem():
            runner = CliRunner()

            @_click.command()
            def _cmd():
                _emit_list(ctx, result, "fake-session-id")

            r = runner.invoke(_cmd)
        assert r.exit_code == 0
        assert "my_list" in r.output
        assert "CONNECTION_FAILED" not in r.output

    def test_emit_list_no_connection_failed_on_bad_value(self):
        """Rendering errors must not be labelled CONNECTION_FAILED."""
        from jupyter_jcli.cli import Context

        ctx = Context(server_url="http://localhost:8888", token=None, use_json=False)
        result = {
            "variables": [{"name": "x", "type": "int", "value": 99}],
            "source": VariableSource.FALLBACK,
        }
        _emit_list(ctx, result, "fake-session-id")


class TestVarsCmdListValuedVariable:
    """End-to-end regression for the originally-reported crash."""

    def test_list_variable_human_output_no_crash(self, live_session, mock_kernel_connection):
        runner = CliRunner()
        mock_kernel_connection.execute("_vl_lst = [1, 2, 3] * 100", timeout=30)
        result = runner.invoke(main, [
            "-s", live_session["url"], "-t", live_session["token"],
            "vars", live_session["session_id"],
        ])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\n"
            f"stdout: {result.output}\nstderr: {result.output}"
        )
        assert "CONNECTION_FAILED" not in (result.output or "")
        assert "_vl_lst" in result.output
