"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import click

from jupyter_jcli.commands.setup_git import git_setup
from jupyter_jcli.commands.setup_hooks import claude, codex
from jupyter_jcli.commands.setup_opencode import opencode


@click.group()
def setup():
    """Install integrations for external tools."""


setup.add_command(claude)
setup.add_command(codex)
setup.add_command(opencode)
setup.add_command(git_setup)
