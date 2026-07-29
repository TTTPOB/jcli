"""jcli — CLI tool for LLM agents to operate Jupyter Lab servers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import click

from jupyter_jcli.config import AppConfig

if TYPE_CHECKING:
    from jupyter_jcli.server import ServerClient


# Keep heavyweight command dependencies out of root CLI startup. Click normally
# resolves every command while formatting help, so descriptions live here too.
_LAZY_COMMANDS = {
    "_hooks": ("jupyter_jcli.commands.hooks_cmd:hooks", "", True),
    "convert": (
        "jupyter_jcli.commands.convert_cmd:convert",
        "Convert between .ipynb and py:percent (.py) formats.",
        False,
    ),
    "exec": (
        "jupyter_jcli.commands.exec_cmd:exec_cmd",
        "Execute code in a session selected by ID, short ID, or name.",
        False,
    ),
    "healthcheck": (
        "jupyter_jcli.commands.healthcheck:healthcheck",
        "Check if the Jupyter server is reachable.",
        False,
    ),
    "kernel": (
        "jupyter_jcli.commands.kernel_cmd:kernel",
        "Manage kernels (interrupt, restart).",
        False,
    ),
    "kernelspec": (
        "jupyter_jcli.commands.kernelspec:kernelspec",
        "Manage kernel specifications.",
        False,
    ),
    "notebook": (
        "jupyter_jcli.commands.notebook:notebook",
        "Inspect notebook cells.",
        False,
    ),
    "serve-cmd": (
        "jupyter_jcli.commands.serve_cmd:serve_cmd",
        "Print a copy-pasteable Jupyter launch command that references env-var token.",
        False,
    ),
    "session": (
        "jupyter_jcli.commands.session:session",
        "Manage Jupyter sessions.",
        False,
    ),
    "setup": (
        "jupyter_jcli.commands.setup_cmd:setup",
        "Install integrations for external tools.",
        False,
    ),
    "vars": (
        "jupyter_jcli.commands.vars_cmd:vars_cmd",
        "Inspect variables in a session selected by ID, short ID, or name.",
        False,
    ),
}


class _LazyGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | _LAZY_COMMANDS.keys())

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        command = super().get_command(ctx, name)
        if command is not None:
            return command
        entry = _LAZY_COMMANDS.get(name)
        if entry is None:
            return None
        module_name, attribute = entry[0].split(":", 1)
        return getattr(import_module(module_name), attribute)

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        rows = [
            (name, description)
            for name, (_target, description, hidden) in sorted(_LAZY_COMMANDS.items())
            if not hidden
        ]
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _ensure_no_proxy(server_url: str) -> None:
    """Ensure local server URLs bypass HTTP proxy."""
    host = urlparse(server_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
        if host not in no_proxy:
            new = f"{no_proxy},{host}" if no_proxy else host
            os.environ["no_proxy"] = new
            os.environ["NO_PROXY"] = new


@dataclass
class CliContext:
    """Shared context passed to all commands."""

    config: AppConfig
    use_json: bool
    server: ServerClient


pass_ctx = click.make_pass_decorator(CliContext)


@click.group(cls=_LazyGroup)
@click.option(
    "--server-url",
    "-s",
    default=None,
    help="Jupyter server URL (env: JCLI_JUPYTER_SERVER_URL, default: http://localhost:8888)",
)
@click.option(
    "--token",
    "-t",
    default=None,
    help="Jupyter server token (env: JCLI_JUPYTER_SERVER_TOKEN)",
)
@click.option(
    "--json",
    "-j",
    "use_json",
    is_flag=True,
    default=False,
    help="Output as JSON for commands; exec --file streams JSON Lines for scripts",
)
@click.version_option(package_name="jupyter-jcli")
@click.pass_context
def main(ctx, server_url, token, use_json):
    """CLI tool for LLM agents to operate Jupyter Lab servers."""
    from jupyter_jcli.server import ServerClient

    config = AppConfig.from_env(server_url=server_url, token=token)
    _ensure_no_proxy(config.server_url)
    ctx.ensure_object(dict)
    ctx.obj = CliContext(
        config=config,
        use_json=use_json,
        server=ServerClient(config.server_url, config.token),
    )
