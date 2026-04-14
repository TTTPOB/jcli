"""Test the variables helper module directly against a live kernel."""

from types import SimpleNamespace

import pytest

from jupyter_jcli.variables import (
    VariablesUnavailable,
    _fallback_list_variables,
    list_variables,
    inspect_variable,
)


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

    def test_returns_dict_shape(self, live_kernel):
        live_kernel.execute("_tv_x = 42; _tv_s = 'hi'; _tv_lst = [1, 2, 3]", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        assert "variables" in result
        assert "source" in result
        assert result["source"] in ("dap", "fallback")
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

        for v in result["variables"]:
            assert "name" in v
            assert "type" in v
            assert "value" in v
            assert "variables_reference" in v


class TestInspectVariable:

    def test_inspect_known_variable(self, live_kernel):
        live_kernel.execute("_ti_x = 42; _ti_s = 'hi'", timeout=30)
        result = inspect_variable(live_kernel, "_ti_x", timeout=15.0)

        assert result["name"] == "_ti_x"
        assert "42" in result["value"]
        assert result["source"] in ("dap", "fallback")

    def test_inspect_missing_variable_raises(self, live_kernel):
        live_kernel.execute("_warmup = 1", timeout=30)
        with pytest.raises(VariablesUnavailable):
            inspect_variable(live_kernel, "__no_such_var__", timeout=15.0)


class TestListVariableValueFields:
    """Integration regression — list_variables always returns string fields."""

    def test_all_fields_are_strings(self, live_kernel):
        live_kernel.execute("_tf_lst = [1, 2, 3] * 100; _tf_x = 42", timeout=30)
        result = list_variables(live_kernel, timeout=15.0)

        for v in result["variables"]:
            assert isinstance(v["name"], str), f"name not str: {v!r}"
            assert isinstance(v["type"], str), f"type not str: {v!r}"
            assert isinstance(v["value"], str), f"value not str: {v!r}"
