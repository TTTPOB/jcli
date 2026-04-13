"""Startup-time regression: tree-sitter must not be imported eagerly.

The lazy-import contract: any CLI path that does NOT invoke a hook
sub-command (healthcheck, session, exec, convert, …) must not pay the
50-150 ms tree-sitter first-import cost.

The test runs in a subprocess so it starts with a pristine module
registry, unaffected by tree-sitter imports from other test modules.
"""

import subprocess
import sys


def test_cli_import_does_not_load_tree_sitter():
    """Importing jupyter_jcli.cli must not pull in tree_sitter or tree_sitter_bash."""
    code = (
        "import jupyter_jcli.cli;"
        " import sys;"
        " bad = [m for m in sys.modules if 'tree_sitter' in m];"
        " assert not bad, f'tree_sitter eagerly imported: {bad}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"tree_sitter was eagerly imported at CLI startup:\n{result.stderr}"
    )


def test_hooks_parser_import_does_not_load_tree_sitter():
    """Importing hooks_parser itself (not calling its functions) must be free."""
    code = (
        "import jupyter_jcli.hooks_parser;"
        " import sys;"
        " bad = [m for m in sys.modules if 'tree_sitter' in m];"
        " assert not bad, f'tree_sitter eagerly imported: {bad}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"tree_sitter was eagerly imported by hooks_parser module-level code:\n"
        f"{result.stderr}"
    )


def test_tree_sitter_loaded_after_first_parse():
    """After calling iter_simple_commands, tree_sitter must appear in sys.modules."""
    code = (
        "from jupyter_jcli.hooks_parser import iter_simple_commands;"
        " iter_simple_commands('echo hi');"
        " import sys;"
        " assert any('tree_sitter' in m for m in sys.modules),"
        " 'tree_sitter was never imported even after first parse'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
