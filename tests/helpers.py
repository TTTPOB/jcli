"""Shared notebook builders and live-kernel synchronization helpers."""

import time

import nbformat


def make_py_text(*sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n",
        "# jupyter:\n",
        "#   kernelspec:\n",
        f"#     display_name: {kernel}\n",
        "#     language: python\n",
        f"#     name: {kernel}\n",
        "# ---\n",
        "\n",
    ]
    for src in sources:
        lines.append("# %%\n")
        lines.append(src + "\n")
        lines.append("\n")
    return "".join(lines)


def make_ipynb_text(*sources: str, kernel: str = "python3") -> str:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "name": kernel,
        "display_name": kernel,
        "language": "python",
    }
    for src in sources:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    return nbformat.writes(nb)


def wait_for_kernel_state(jupyter_server, session_id, expected, timeout=10):
    from jupyter_jcli.server import ServerClient

    server = ServerClient(jupyter_server["url"], jupyter_server["token"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sessions = server.list_sessions()
        session = next(
            (item for item in sessions if item["session_id"] == session_id), None
        )
        if session is not None and session["kernel_state"] == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"kernel did not reach {expected!r} within {timeout}s")
