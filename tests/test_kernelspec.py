"""Test kernelspec commands."""

import json

import nbformat
from click.testing import CliRunner

from jupyter_jcli.cli import main


def test_kernelspec_list_human(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "kernelspec",
            "list",
        ],
    )
    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "python3" in result.output


def test_kernelspec_list_json(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "-s",
            jupyter_server["url"],
            "-t",
            jupyter_server["token"],
            "--json",
            "kernelspec",
            "list",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "kernelspecs" in data
    names = [s["name"] for s in data["kernelspecs"]]
    assert "python3" in names


def test_kernelspec_inspect_file_py_percent_json(tmp_path):
    py_file = tmp_path / "analysis.py"
    py_file.write_text(
        "# ---\n"
        "# jupyter:\n"
        "#   kernelspec:\n"
        "#     display_name: R 4.3\n"
        "#     language: R\n"
        "#     name: ir\n"
        "# ---\n"
        "\n"
        "# %%\n"
        "x <- 1\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--json",
            "kernelspec",
            "inspect-file",
            str(py_file),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {
        "path": str(py_file),
        "kernel_name": "ir",
        "kernel_display_name": "R 4.3",
        "kernel_language": "R",
    }


def test_kernelspec_inspect_file_ipynb_json(tmp_path):
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "name": "julia-1.10",
        "display_name": "Julia 1.10",
        "language": "julia",
    }
    nb.cells = [nbformat.v4.new_code_cell("1 + 1")]
    ipynb = tmp_path / "analysis.ipynb"
    nbformat.write(nb, str(ipynb))

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--json",
            "kernelspec",
            "inspect-file",
            str(ipynb),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {
        "path": str(ipynb),
        "kernel_name": "julia-1.10",
        "kernel_display_name": "Julia 1.10",
        "kernel_language": "julia",
    }


def test_kernelspec_inspect_file_human_outputs_kernel_name(tmp_path):
    py_file = tmp_path / "analysis.py"
    py_file.write_text(
        "# ---\n"
        "# jupyter:\n"
        "#   kernelspec:\n"
        "#     name: python3\n"
        "# ---\n"
        "\n"
        "# %%\n"
        "print(1)\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "kernelspec",
            "inspect-file",
            str(py_file),
        ],
    )

    assert result.exit_code == 0
    assert result.output == "python3\n"


def test_kernelspec_inspect_file_no_kernel_json(tmp_path):
    py_file = tmp_path / "plain.py"
    py_file.write_text("print(1)\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--json",
            "kernelspec",
            "inspect-file",
            str(py_file),
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["kernel_name"] is None
    assert data["kernel_display_name"] is None
    assert data["kernel_language"] is None


def test_kernelspec_inspect_file_missing_path_exits_nonzero(tmp_path):
    missing = tmp_path / "missing.py"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "kernelspec",
            "inspect-file",
            str(missing),
        ],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output
