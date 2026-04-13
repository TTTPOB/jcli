"""Test the variables helper module directly against a live kernel."""

import json
from types import SimpleNamespace

import pytest

from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.kernel import kernel_connection
from jupyter_jcli.server import get_kernel_id_for_session
from jupyter_jcli.variables import (
    VariablesUnavailable,
    _fallback_list_variables,
    list_variables,
    inspect_variable,
)


def _create_session(runner, url, token):
    result = runner.invoke(main, [
        "-s", url, "-t", token, "--json",
        "session", "create", "--kernel", "python3",
    ])
    return json.loads(result.output)


def _kill_session(runner, url, token, sid):
    runner.invoke(main, ["-s", url, "-t", token, "session", "kill", sid])


class TestFallbackListVariablesNormalisation:
    """Unit tests — no live kernel needed."""

    def test_dict_branch_coerces_to_str(self):
        class _FakeKernel:
            def list_variables(self):
                return [{"name": "lst", "type": "list", "value": [1, 2, 3]}]

        result = _fallback_list_variables(_FakeKernel())
        assert len(result) == 1
        v = result[0]
        assert isinstance(v["name"], str)
        assert isinstance(v["type"], str)
        assert isinstance(v["value"], str)

    def test_attr_branch_coerces_to_str(self):
        class _FakeKernel:
            def list_variables(self):
                return [SimpleNamespace(name="arr", type="ndarray", value=[10, 20])]

        result = _fallback_list_variables(_FakeKernel())
        assert len(result) == 1
        v = result[0]
        assert isinstance(v["name"], str)
        assert isinstance(v["type"], str)
        assert isinstance(v["value"], str)


class TestListVariables:

    def test_returns_dict_shape(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                # Warm up the kernel first, then seed variables
                kernel.execute("x = 42; s = 'hi'; lst = [1, 2, 3]", timeout=30)
                result = list_variables(kernel, timeout=15.0)

            assert "variables" in result
            assert "source" in result
            assert result["source"] in ("dap", "fallback")
            assert isinstance(result["variables"], list)
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_user_variables_present(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                kernel.execute("x = 42; s = 'hi'; lst = [1, 2, 3]", timeout=30)
                result = list_variables(kernel, timeout=15.0)

            names = [v["name"] for v in result["variables"]]
            assert "x" in names
            assert "s" in names
            assert "lst" in names

            x_var = next(v for v in result["variables"] if v["name"] == "x")
            assert "42" in x_var["value"]
            assert "int" in x_var["type"].lower()
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_variable_dict_fields(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                kernel.execute("x = 42", timeout=30)
                result = list_variables(kernel, timeout=15.0)

            for v in result["variables"]:
                assert "name" in v
                assert "type" in v
                assert "value" in v
                assert "variables_reference" in v
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)


class TestInspectVariable:

    def test_inspect_known_variable(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                kernel.execute("x = 42; s = 'hi'", timeout=30)
                result = inspect_variable(kernel, "x", timeout=15.0)

            assert result["name"] == "x"
            assert "42" in result["value"]
            assert result["source"] in ("dap", "fallback")
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)

    def test_inspect_missing_variable_raises(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                # Ensure kernel is warm
                kernel.execute("_warmup = 1", timeout=30)
                with pytest.raises(VariablesUnavailable):
                    inspect_variable(kernel, "__no_such_var__", timeout=15.0)
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)


class TestListVariableValueFields:
    """Integration regression — list_variables always returns string fields."""

    def test_all_fields_are_strings(self, jupyter_server):
        runner = CliRunner()
        info = _create_session(runner, jupyter_server["url"], jupyter_server["token"])
        sid = info["session_id"]
        try:
            kernel_id = get_kernel_id_for_session(
                jupyter_server["url"], sid, jupyter_server["token"]
            )
            with kernel_connection(
                jupyter_server["url"], jupyter_server["token"], kernel_id
            ) as kernel:
                kernel.execute("lst = [1, 2, 3] * 100; x = 42", timeout=30)
                result = list_variables(kernel, timeout=15.0)

            for v in result["variables"]:
                assert isinstance(v["name"], str), f"name not str: {v!r}"
                assert isinstance(v["type"], str), f"type not str: {v!r}"
                assert isinstance(v["value"], str), f"value not str: {v!r}"
        finally:
            _kill_session(runner, jupyter_server["url"], jupyter_server["token"], sid)
