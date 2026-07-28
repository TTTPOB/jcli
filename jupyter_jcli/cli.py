"""jcli — CLI tool for LLM agents to operate Jupyter Lab servers."""

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import click

from jupyter_jcli.config import AppConfig


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

    def resolve_session(self, selector: str) -> str:
        """Resolve a session selector using this context's server connection."""
        from jupyter_jcli.session_selector import resolve_session_selector

        return resolve_session_selector(
            self.config.server_url, selector, self.config.token
        )

    def resolve_kernel(self, selector: str) -> tuple[str, str]:
        """Resolve a session selector and return its session and kernel IDs."""
        from jupyter_jcli.server import get_kernel_id_for_session

        session_id = self.resolve_session(selector)
        kernel_id = get_kernel_id_for_session(
            self.config.server_url, session_id, self.config.token
        )
        return session_id, kernel_id


pass_ctx = click.make_pass_decorator(CliContext)


@click.group()
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
    config = AppConfig.from_env(server_url=server_url, token=token)
    _ensure_no_proxy(config.server_url)
    ctx.ensure_object(dict)
    ctx.obj = CliContext(config=config, use_json=use_json)


# Import and register command groups
from jupyter_jcli.commands.convert_cmd import convert
from jupyter_jcli.commands.exec_cmd import exec_cmd
from jupyter_jcli.commands.healthcheck import healthcheck
from jupyter_jcli.commands.hooks_cmd import hooks
from jupyter_jcli.commands.kernel_cmd import kernel
from jupyter_jcli.commands.kernelspec import kernelspec
from jupyter_jcli.commands.notebook import notebook
from jupyter_jcli.commands.serve_cmd import serve_cmd
from jupyter_jcli.commands.session import session
from jupyter_jcli.commands.setup_cmd import setup
from jupyter_jcli.commands.vars_cmd import vars_cmd

main.add_command(healthcheck)
main.add_command(kernelspec)
main.add_command(session)
main.add_command(kernel)
main.add_command(exec_cmd, name="exec")
main.add_command(setup)
main.add_command(hooks, name="_hooks")
main.add_command(convert)
main.add_command(vars_cmd, name="vars")
main.add_command(serve_cmd, name="serve-cmd")
main.add_command(notebook)
