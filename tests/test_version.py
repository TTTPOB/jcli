"""Tests for CLI version reporting."""

from importlib.metadata import version

from click.testing import CliRunner

from jupyter_jcli import __version__
from jupyter_jcli.cli import main


def test_version_uses_installed_distribution_metadata():
    expected = version("jupyter-jcli")
    runner = CliRunner()
    result = runner.invoke(main, ["--version"], prog_name="j-cli", catch_exceptions=False)

    assert result.exit_code == 0
    assert "j-cli" in result.output
    assert expected in result.output
    assert __version__ == expected
