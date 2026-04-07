"""jcli — CLI tool for LLM agents to operate Jupyter Lab servers."""

import os
from urllib.parse import urlparse

import click

from jupyter_jcli.config import get_server_url, get_token


def _ensure_no_proxy(server_url: str) -> None:
    """Ensure local server URLs bypass HTTP proxy."""
    host = urlparse(server_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
        if host not in no_proxy:
            new = f"{no_proxy},{host}" if no_proxy else host
            os.environ["no_proxy"] = new
            os.environ["NO_PROXY"] = new


class Context:
    """Shared context passed to all commands."""

    def __init__(self, server_url: str, token: str | None, use_json: bool):
        self.server_url = server_url
        self.token = token
        self.use_json = use_json


pass_ctx = click.make_pass_decorator(Context)


@click.group()
@click.option(
    "--server-url", "-s", default=None,
    help="Jupyter server URL (env: JCLI_JUPYTER_SERVER_URL, default: http://localhost:8888)",
)
@click.option(
    "--token", "-t", default=None,
    help="Jupyter server token (env: JCLI_JUPYTER_SERVER_TOKEN)",
)
@click.option(
    "--json", "-j", "use_json", is_flag=True, default=False,
    help="Output as JSON instead of human-readable text",
)
@click.version_option(package_name="jcli")
@click.pass_context
def main(ctx, server_url, token, use_json):
    """CLI tool for LLM agents to operate Jupyter Lab servers."""
    resolved_url = get_server_url(server_url)
    _ensure_no_proxy(resolved_url)
    ctx.ensure_object(dict)
    ctx.obj = Context(
        server_url=resolved_url,
        token=get_token(token),
        use_json=use_json,
    )


# Import and register command groups
from jupyter_jcli.commands.healthcheck import healthcheck  # noqa: E402
from jupyter_jcli.commands.kernelspec import kernelspec  # noqa: E402
from jupyter_jcli.commands.session import session  # noqa: E402
from jupyter_jcli.commands.kernel_cmd import kernel  # noqa: E402
from jupyter_jcli.commands.exec_cmd import exec_cmd  # noqa: E402

main.add_command(healthcheck)
main.add_command(kernelspec)
main.add_command(session)
main.add_command(kernel)
main.add_command(exec_cmd, name="exec")
