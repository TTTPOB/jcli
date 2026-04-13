"""Test the 'vars' subcommand end-to-end against a live kernel."""

import json

import pytest

from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.commands.vars_cmd import _emit_list


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token, "--json",
        "session", "create", "--kernel", "python3",
    ])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _kill_session(runner, url, token, sid):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", sid])


def _exec(runner, url, token, sid, code):
    result = runner.invoke(main, [
        "-s", url, "-t", token,
        "exec", sid, "--code", code,
    ])
    return result


class TestVarsCmdList:

    def test_json_output_shape(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid,
                  "x = 42; s = 'hi'")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "vars", sid,
            ])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert "variables" in data
            assert "source" in data
            assert data["source"] in ("dap", "fallback")
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_user_variables_in_output(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid,
                  "x = 42; s = 'hi'")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "vars", sid,
            ])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            names = [v["name"] for v in data["variables"]]
            assert "x" in names
            assert "s" in names

            x_var = next(v for v in data["variables"] if v["name"] == "x")
            assert "42" in x_var["value"]
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_human_output_table(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid,
                  "my_var = 99")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "vars", sid,
            ])
            assert result.exit_code == 0, result.output
            assert "my_var" in result.output
            assert "99" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)


class TestVarsCmdSingleVar:

    def test_inspect_single_variable_json(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid,
                  "x = 42")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "vars", sid, "--name", "x",
            ])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["name"] == "x"
            assert "42" in data["value"]
            assert data["source"] in ("dap", "fallback")
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_inspect_missing_variable_exits_1(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            # Warm up the kernel
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid, "pass")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "vars", sid, "--name", "__no_such_var__",
            ])
            assert result.exit_code == 1
            err = json.loads(result.output if result.output else "{}")
            # Error is written to stderr by emit_error, output may be empty
            assert result.exit_code == 1
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_rich_requires_name(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "--json", "vars", sid, "--rich",
            ])
            assert result.exit_code == 1
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)


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
            "source": "fallback",
        }
        # Must not raise TypeError
        _emit_list(ctx, result, "fake-session-id")

    def test_emit_list_output_contains_variable_name(self):
        from io import StringIO
        import click
        from jupyter_jcli.cli import Context

        ctx = Context(server_url="http://localhost:8888", token=None, use_json=False)
        result = {
            "variables": [{"name": "my_list", "type": "list", "value": [1, 2, 3]}],
            "source": "fallback",
        }
        output_lines = []
        with CliRunner().isolated_filesystem():
            runner = CliRunner()
            # Invoke through a thin wrapper to capture output
            import click as _click

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
            "source": "fallback",
        }
        # Should not raise; the str() cast handles the non-string int value
        _emit_list(ctx, result, "fake-session-id")


class TestVarsCmdListValuedVariable:
    """End-to-end regression for the originally-reported crash."""

    def test_list_variable_human_output_no_crash(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            _exec(runner, jupyter_server["url"], jupyter_server["token"], sid,
                  "lst = [1, 2, 3] * 100")
            result = runner.invoke(main, [
                "-s", jupyter_server["url"], "-t", jupyter_server["token"],
                "vars", sid,
            ])
            assert result.exit_code == 0, (
                f"Expected exit 0, got {result.exit_code}.\n"
                f"stdout: {result.output}\nstderr: {result.output}"
            )
            assert "CONNECTION_FAILED" not in (result.output or "")
            assert "lst" in result.output
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)
