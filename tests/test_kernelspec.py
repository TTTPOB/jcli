"""Test kernelspec commands."""

import json

from click.testing import CliRunner

from jupyter_jcli.cli import main


def test_kernelspec_list_human(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "kernelspec", "list",
    ])
    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "python3" in result.output


def test_kernelspec_list_json(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "-s", jupyter_server["url"], "-t", jupyter_server["token"],
        "--json", "kernelspec", "list",
    ])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "kernelspecs" in data
    names = [s["name"] for s in data["kernelspecs"]]
    assert "python3" in names
