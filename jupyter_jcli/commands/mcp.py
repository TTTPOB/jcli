"""MCP commands, kept dependency-free until the server starts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click


@click.group("mcp")
def mcp() -> None:
    """Serve read-only tools over Model Context Protocol."""


@mcp.command("serve")
@click.option(
    "--root",
    "roots",
    multiple=True,
    metavar="ABS_PATH",
    help="Trusted filesystem root; repeat for multiple roots.",
)
def serve(roots: tuple[str, ...]) -> None:
    """Serve saved notebook outputs over stdio."""
    resolved_roots = tuple(_explicit_root(value) for value in roots)
    try:
        from jupyter_jcli.mcp_server import serve_stdio
    except ModuleNotFoundError as error:
        if error.name == "mcp" or (error.name or "").startswith("mcp."):
            raise click.ClickException(
                "MCP support is not installed; reinstall jupyter-jcli"
            ) from error
        raise
    asyncio.run(serve_stdio(resolved_roots))


def _explicit_root(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise click.BadParameter("must be an absolute path", param_hint="--root")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise click.BadParameter(
            f"directory does not exist: {resolved}", param_hint="--root"
        )
    return resolved
