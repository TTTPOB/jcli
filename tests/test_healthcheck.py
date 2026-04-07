"""Test healthcheck command."""

from click.testing import CliRunner

from jcli.cli import main


def test_healthcheck_human(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "--server-url", jupyter_server["url"],
        "--token", jupyter_server["token"],
        "healthcheck",
    ])
    assert result.exit_code == 0
    assert "OK" in result.output
    assert "Jupyter server" in result.output


def test_healthcheck_json(jupyter_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        "--server-url", jupyter_server["url"],
        "--token", jupyter_server["token"],
        "--json",
        "healthcheck",
    ])
    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert "version" in data
    assert "kernels_running" in data
