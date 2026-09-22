"""Structural regressions for setup host command ownership."""

import ast
import inspect

from jupyter_jcli.commands.setup import hooks
from jupyter_jcli.commands.setup.command import setup


def test_each_host_command_is_registered_from_its_adapter_module():
    for host in ("claude", "codex", "dsh", "opencode"):
        command = setup.commands[host]
        assert inspect.unwrap(command.callback).__module__ == (
            f"jupyter_jcli.commands.setup.{host}"
        )


def test_shared_hooks_backend_has_no_click_or_cli_context_dependency():
    tree = ast.parse(inspect.getsource(hooks))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "click" not in imported_names
    assert "jupyter_jcli.cli" not in imported_modules
    assert {"CliContext", "pass_ctx"}.isdisjoint(imported_names)
    assert not hasattr(hooks, "claude")
    assert not hasattr(hooks, "codex")
