"""Pytest wrapper for the native DSH plugin Node contract tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required"
)


def test_dsh_plugin_node_contract() -> None:
    """Run the single-file TypeScript plugin under Node's native type stripping."""
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--test",
            str(root / "tests" / "js" / "dsh_plugin.test.mjs"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"node tests failed:\n{result.stdout}\n{result.stderr}"
    )
