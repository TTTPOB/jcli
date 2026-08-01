"""Tests for the kernel execution dependency boundary."""

import subprocess
import sys


def test_kernel_module_imports_required_dependency_api():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from jupyter_jcli.kernel import "
                "KernelClient, KernelWebSocketClient, output_hook"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
