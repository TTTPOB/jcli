"""Bun contract runner for the OpenCode notebook output tool."""

import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="Bun is required")


def test_opencode_output_tool_contracts():
    root = Path(__file__).parent
    env = {
        **os.environ,
        "JCLI_TEST_PLUGIN": str(
            resources.files("jupyter_jcli").joinpath("opencode_plugin.js")
        ),
        "JCLI_OUTPUT_FIXTURE": str(
            root / "fixtures" / "outputs" / "mixed_outputs.json"
        ),
    }
    subprocess.run(
        ["bun", "test", str(root / "js" / "opencode_plugin.test.mjs")],
        check=True,
        env=env,
    )
