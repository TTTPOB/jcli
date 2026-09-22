"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import click

from .claude import claude
from .codex import codex
from .dsh import dsh
from .git import git_setup
from .opencode import opencode


@click.group()
def setup():
    """Install integrations for external tools."""


setup.add_command(claude)
setup.add_command(codex)
setup.add_command(dsh)
setup.add_command(opencode)
setup.add_command(git_setup)
