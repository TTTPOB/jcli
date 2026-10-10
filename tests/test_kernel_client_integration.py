"""Real WebSocket regressions for the independently packaged kernel client."""

from copy import deepcopy

import pytest
from jcli_kernel_client import KernelClient

from jupyter_jcli.kernel import (
    _wait_for_kernel_websocket_ready,
    execute_with_timeout,
    kernel_connection,
)
from jupyter_jcli.server import ServerClient
from jupyter_jcli.variables import (
    VariableSource,
    _fallback_list_variables,
    inspect_variable,
    list_variables,
)


def test_default_factory_stop_preserves_kernel_and_allows_reattach(jupyter_server):
    server = ServerClient(jupyter_server["url"], jupyter_server["token"])
    session = server.create_session("python3")
    kernel = KernelClient(
        server_url=jupyter_server["url"],
        token=jupyter_server["token"],
        kernel_id=session["kernel_id"],
    )
    try:
        # Do not inject jcli's factory: exercise the fork's traitlets default.
        kernel.start(timeout=15)
        _wait_for_kernel_websocket_ready(kernel, timeout=15)
        client = kernel._manager.client
        thread = client.connection_thread
        assert thread is not None and thread.is_alive()
        result = execute_with_timeout(kernel, "fork_attach_sentinel = 41", timeout=10)
        assert result["status"] == "ok"

        kernel.stop()
        thread.join(timeout=3)
        assert not thread.is_alive(), "default WebSocket listener survived stop()"
        assert (
            server.get_kernel_id_for_session(session["session_id"])
            == session["kernel_id"]
        )
        with kernel_connection(
            jupyter_server["url"], jupyter_server["token"], session["kernel_id"]
        ) as reattached:
            result = execute_with_timeout(
                reattached, "fork_attach_sentinel + 1", timeout=10
            )
        assert result["status"] == "ok"
        assert result["outputs"][-1]["data"]["text/plain"] == "42"
    finally:
        kernel.stop()
        server.delete_session(session["session_id"])


@pytest.mark.parametrize("wait", [False, True])
def test_real_display_update_and_clear(live_kernel, wait):
    observations = []

    def observe(message, outputs, changed, execution_count):
        if message is not None:
            observations.append((message, deepcopy(outputs), set(changed)))

    result = execute_with_timeout(
        live_kernel,
        "from IPython.display import display, update_display, clear_output\n"
        "display({'text/plain': 'before'}, raw=True, display_id='fork-output')\n"
        "update_display({'text/plain': 'updated'}, raw=True, display_id='fork-output')\n"
        f"clear_output(wait={wait!r})\n"
        "print('after clear', flush=True)",
        timeout=10,
        observer=observe,
    )
    assert result["status"] == "ok"
    update = next(
        item
        for item in observations
        if item[0]["header"]["msg_type"] == "update_display_data"
    )
    assert len(update[1]) == 1
    assert update[1][0]["data"]["text/plain"] == "updated"
    assert update[2] == {0}
    clear = next(
        item
        for item in observations
        if item[0]["header"]["msg_type"] == "clear_output"
        and item[0]["content"]["wait"] is wait
    )
    assert len(clear[1]) == (1 if wait else 0)
    assert result["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": "after clear\n"}
    ]


def test_real_wait_clear_preserves_display_until_new_output(live_kernel):
    result = execute_with_timeout(
        live_kernel,
        "from IPython.display import display, update_display, clear_output\n"
        "display({'text/plain': 'before'}, raw=True, display_id='fork-deferred')\n"
        "clear_output(wait=True)\n"
        "update_display({'text/plain': 'updated'}, raw=True, display_id='fork-deferred')",
        timeout=10,
    )
    assert result["status"] == "ok"
    assert len(result["outputs"]) == 1
    assert result["outputs"][0]["data"]["text/plain"] == "updated"


def test_real_dap_list_and_rich_inspection(live_kernel):
    result = execute_with_timeout(
        live_kernel,
        "fork_dap_integer = 42; fork_dap_text = 'hello'; fork_dap_list = [1, 2, 3]",
        timeout=10,
    )
    assert result["status"] == "ok"
    variables = list_variables(live_kernel, timeout=15)
    assert variables["source"] is VariableSource.DAP
    by_name = {variable["name"]: variable for variable in variables["variables"]}
    assert {"fork_dap_integer", "fork_dap_text", "fork_dap_list"} <= by_name.keys()
    assert by_name["fork_dap_integer"]["value"] == "42"
    inspected = inspect_variable(live_kernel, "fork_dap_integer", rich=True, timeout=15)
    assert inspected["source"] is VariableSource.DAP
    assert inspected["value"] == "42"
    assert inspected["data"]["text/plain"] == "42"


def test_real_shell_variable_fallback(live_kernel):
    result = execute_with_timeout(
        live_kernel,
        "fork_fallback_integer = 42; fork_fallback_text = 'hello'; "
        "fork_fallback_list = [1, 2, 3]",
        timeout=10,
    )
    assert result["status"] == "ok"
    # Execute the actual snippet rather than replacing DAP with a mock failure.
    variables = _fallback_list_variables(live_kernel, timeout=15)
    by_name = {variable["name"]: variable for variable in variables}
    assert {"fork_fallback_integer", "fork_fallback_text", "fork_fallback_list"} <= (
        by_name.keys()
    )
    # The shell snippet supplies metadata only; DAP supplies value previews.
    for name, type_name in (
        ("fork_fallback_integer", "int"),
        ("fork_fallback_text", "str"),
        ("fork_fallback_list", "list"),
    ):
        assert by_name[name] == {
            "name": name,
            "type": str(["builtins", type_name]),
            "value": "",
            "variables_reference": 0,
        }
    assert all(variable["variables_reference"] == 0 for variable in variables)
