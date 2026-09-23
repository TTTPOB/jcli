"""Test the variables helper module directly against a live kernel."""

from types import SimpleNamespace

import pytest

import jupyter_jcli.variables as variables_module
from jupyter_jcli.variables import (
    VariableSource,
    VariablesUnavailable,
    _fallback_list_variables,
    inspect_variable,
    list_variables,
)


class TestFallbackListVariablesNormalisation:
    """Unit tests — no live kernel needed."""

    def test_dict_branch_coerces_to_str(self, monkeypatch):
        kernel = SimpleNamespace(kernel_info={"language_info": {"name": "python"}})
        monkeypatch.setattr(
            variables_module,
            "execute_with_timeout",
            lambda *args, **kwargs: {
                "status": "ok",
                "outputs": [
                    {
                        "data": {
                            "application/json": [
                                {"name": "lst", "type": "list", "value": [1, 2, 3]}
                            ]
                        }
                    }
                ],
            },
        )

        result = _fallback_list_variables(kernel, timeout=1.5)
        assert len(result) == 1
        v = result[0]
        assert isinstance(v["name"], str)
        assert isinstance(v["type"], str)
        assert isinstance(v["value"], str)

    def test_attr_branch_coerces_to_str(self, monkeypatch):
        kernel = SimpleNamespace(kernel_info={"language_info": {"name": "python"}})
        monkeypatch.setattr(
            variables_module,
            "execute_with_timeout",
            lambda *args, **kwargs: {
                "status": "ok",
                "outputs": [
                    {
                        "data": {
                            "application/json": [
                                SimpleNamespace(
                                    name="arr", type="ndarray", value=[10, 20]
                                )
                            ]
                        }
                    }
                ],
            },
        )

        result = _fallback_list_variables(kernel, timeout=1.5)
        assert len(result) == 1
        v = result[0]
        assert isinstance(v["name"], str)
        assert isinstance(v["type"], str)
        assert isinstance(v["value"], str)


class TestFallbackTimeout:
    @pytest.mark.parametrize("inspect", [False, True])
    def test_requested_timeout_reaches_shell_execution(self, monkeypatch, inspect):
        kernel = SimpleNamespace(kernel_info={"language_info": {"name": "python"}})
        calls = []

        def execute(kernel_arg, snippet, *, timeout, silent):
            calls.append((kernel_arg, snippet, timeout, silent))
            return {
                "status": "ok",
                "outputs": [
                    {
                        "data": {
                            "application/json": [
                                {"name": "answer", "type": "int", "value": "42"}
                            ]
                        }
                    }
                ],
            }

        monkeypatch.setattr(variables_module, "execute_with_timeout", execute)
        if inspect:
            result = inspect_variable(kernel, "answer", timeout=0.25)
            assert result["name"] == "answer"
        else:
            result = list_variables(kernel, timeout=0.25)
            assert result["variables"][0]["name"] == "answer"
        assert result["source"] is VariableSource.FALLBACK
        assert len(calls) == 1
        assert calls[0][0] is kernel
        assert calls[0][1] == variables_module.SNIPPETS_REGISTRY.get_list_variables(
            "python"
        )
        assert calls[0][2:] == (0.25, True)

    def test_dap_failure_uses_same_timeout_for_fallback(self, monkeypatch):
        kernel = SimpleNamespace(
            kernel_info={
                "supported_features": ["debugger"],
                "language_info": {"name": "python"},
            },
            _manager=SimpleNamespace(client=object()),
        )

        def dap_timeout(wsc, *, timeout):
            assert timeout == 0.2
            raise TimeoutError("DAP deadline expired")

        monkeypatch.setattr(variables_module, "_dap_inspect_variables", dap_timeout)
        calls = []
        monkeypatch.setattr(
            variables_module,
            "execute_with_timeout",
            lambda kernel, snippet, *, timeout, silent: (
                calls.append(timeout)
                or {
                    "status": "ok",
                    "outputs": [{"data": {"application/json": []}}],
                }
            ),
        )
        assert list_variables(kernel, timeout=0.2)["source"] is VariableSource.FALLBACK
        assert calls == [0.2]

    def test_shell_timeout_is_reported_unavailable(self, monkeypatch):
        kernel = SimpleNamespace(kernel_info={"language_info": {"name": "python"}})

        def time_out(*args, **kwargs):
            raise TimeoutError("deadline expired")

        monkeypatch.setattr(variables_module, "execute_with_timeout", time_out)
        with pytest.raises(VariablesUnavailable, match="deadline expired"):
            list_variables(kernel, timeout=0.2)


class TestListVariables:
    def test_returns_dict_shape(self, live_kernel):
        live_kernel.execute("_tv_x = 42; _tv_s = 'hi'; _tv_lst = [1, 2, 3]", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        assert "variables" in result
        assert "source" in result
        assert result["source"] in (VariableSource.DAP, VariableSource.FALLBACK)
        assert isinstance(result["variables"], list)

    def test_user_variables_present(self, live_kernel):
        live_kernel.execute("_tv_x = 42; _tv_s = 'hi'; _tv_lst = [1, 2, 3]", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        names = [v["name"] for v in result["variables"]]
        assert "_tv_x" in names
        assert "_tv_s" in names
        assert "_tv_lst" in names

        x_var = next(v for v in result["variables"] if v["name"] == "_tv_x")
        assert "42" in x_var["value"]
        assert "int" in x_var["type"].lower()

    def test_variable_dict_fields(self, live_kernel):
        live_kernel.execute("_tv_field_x = 42", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        variable = next(v for v in result["variables"] if v["name"] == "_tv_field_x")
        assert {"name", "type", "value", "variables_reference"} <= variable.keys()


class TestInspectVariable:
    def test_inspect_known_variable(self, live_kernel):
        live_kernel.execute("_ti_x = 42; _ti_s = 'hi'", timeout=30)
        result = inspect_variable(live_kernel, "_ti_x", timeout=15.0)

        assert result["name"] == "_ti_x"
        assert "42" in result["value"]
        assert result["source"] in (VariableSource.DAP, VariableSource.FALLBACK)

    def test_inspect_missing_variable_raises(self, live_kernel):
        live_kernel.execute("_warmup = 1", timeout=30)
        with pytest.raises(VariablesUnavailable):
            inspect_variable(live_kernel, "__no_such_var__", timeout=15.0)


class TestVariableSourceEnum:
    """Unit tests — VariableSource enum value stability."""

    def test_dap_value_equals_string(self):
        assert VariableSource.DAP == "dap"

    def test_fallback_round_trips(self):
        assert VariableSource("fallback") is VariableSource.FALLBACK


class TestListVariableValueFields:
    """Integration regression — list_variables always returns string fields."""

    def test_all_fields_are_strings(self, live_kernel):
        live_kernel.execute("_tf_lst = [1, 2, 3] * 100; _tf_x = 42", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        variables = [
            v for v in result["variables"] if v["name"] in {"_tf_lst", "_tf_x"}
        ]
        assert {v["name"] for v in variables} == {"_tf_lst", "_tf_x"}
        for v in variables:
            assert isinstance(v["name"], str), f"name not str: {v!r}"
            assert isinstance(v["type"], str), f"type not str: {v!r}"
            assert isinstance(v["value"], str), f"value not str: {v!r}"
