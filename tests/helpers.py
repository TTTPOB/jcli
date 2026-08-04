"""Shared helpers for pair drift / notebook tests."""

import nbformat


def make_py_text(*sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n",
        "# jupyter:\n",
        "#   kernelspec:\n",
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
