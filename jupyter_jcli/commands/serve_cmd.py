"""jcli serve-cmd — print a Jupyter launch command with env-var token reference."""

import re
import shlex
from enum import Enum
from urllib.parse import urlparse

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


class ServeBackend(str, Enum):
    """Supported Jupyter backend subcommands."""
    LAB = "lab"
    SERVER = "server"
    NOTEBOOK = "notebook"

# Hostname may only contain alphanumerics, dots, hyphens, and brackets (IPv6).
_SAFE_HOST_RE = re.compile(r"^[a-zA-Z0-9._\-\[\]]+$")

_SCHEME_PORTS = {"http": 80, "https": 443}


@click.command("serve-cmd")
@click.option(
    "--serve-backend", required=True,
    type=click.Choice([e.value for e in ServeBackend], case_sensitive=True),
    help="Jupyter backend to launch.",
)
@click.option("--ip", default=None, help="Override ServerApp.ip (default: from JCLI_JUPYTER_SERVER_URL).")
@click.option("--port", default=None, type=int, help="Override ServerApp.port (default: from JCLI_JUPYTER_SERVER_URL).")
@click.option("--root-dir", default=None, help="Set ServerApp.root_dir (shell-quoted in output).")
@click.option(
    "--no-browser/--browser", "no_browser", default=True,
    help="Pass --no-browser to Jupyter (default: on).",
)
@pass_ctx
def serve_cmd(
    ctx: Context,
    serve_backend: str,
    ip: str | None,
    port: int | None,
    root_dir: str | None,
    no_browser: bool,
) -> None:
    """Print a copy-pasteable Jupyter launch command that references env-var token.

    The token is never inlined; the output always contains the literal string
    "$JCLI_JUPYTER_SERVER_TOKEN" so the shell expands it at paste time.

    \b
    Example
    -------
    $ export JCLI_JUPYTER_SERVER_TOKEN=mysecret
    $ j-cli serve-cmd --serve-backend lab
    jupyter lab --ServerApp.token="$JCLI_JUPYTER_SERVER_TOKEN" \\
        --ServerApp.ip=localhost --ServerApp.port=8888 --no-browser
    """
    # Confirm token is available without inlining its value
    if ctx.token is None:
        emit_error(
            "SERVE_CMD_NO_TOKEN",
            "JCLI_JUPYTER_SERVER_TOKEN is not set. "
            "Export it before using serve-cmd.",
            ctx.use_json,
        )
        return

    # Resolve hostname
    parsed = urlparse(ctx.server_url)
    if ip is None:
        raw_host = parsed.hostname or ""
        if not raw_host or not _SAFE_HOST_RE.match(raw_host):
            emit_error(
                "SERVE_CMD_BAD_URL",
                f"Cannot parse a safe hostname from URL: {ctx.server_url!r}",
                ctx.use_json,
            )
            return
        resolved_host = raw_host
    else:
        if not _SAFE_HOST_RE.match(ip):
            emit_error(
                "SERVE_CMD_BAD_URL",
                f"--ip value contains unsafe characters: {ip!r}",
                ctx.use_json,
            )
            return
        resolved_host = ip

    # Resolve port
    resolved_port: int = port if port is not None else (
        parsed.port or _SCHEME_PORTS.get(parsed.scheme, 80)
    )

    # Build the shell command string.  Token reference uses double quotes so
    # the shell expands $JCLI_JUPYTER_SERVER_TOKEN at paste time.  Host and
    # port go through shlex.quote as a safety measure; root_dir may have spaces.
    parts = [
        "jupyter",
        serve_backend,
        '--ServerApp.token="$JCLI_JUPYTER_SERVER_TOKEN"',
        f"--ServerApp.ip={shlex.quote(resolved_host)}",
        f"--ServerApp.port={shlex.quote(str(resolved_port))}",
    ]
    if root_dir:
        parts.append(f"--ServerApp.root_dir={shlex.quote(root_dir)}")
    if no_browser:
        parts.append("--no-browser")

    command = " ".join(parts)

    # argv_template: structured form without shell quoting, for programmatic use
    argv_template = [
        "jupyter",
        serve_backend,
        "--ServerApp.token=$JCLI_JUPYTER_SERVER_TOKEN",
        f"--ServerApp.ip={resolved_host}",
        f"--ServerApp.port={resolved_port}",
    ]
    if root_dir:
        argv_template.append(f"--ServerApp.root_dir={root_dir}")
    if no_browser:
        argv_template.append("--no-browser")

    if ctx.use_json:
        emit(
            {
                "status": ResponseStatus.OK,
                "command": command,
                "argv_template": argv_template,
                "env_refs": ["JCLI_JUPYTER_SERVER_TOKEN"],
            },
            use_json=True,
        )
        return

    # Human mode: hint to stderr so stdout is pipe-safe
    click.echo(
        "# paste this into a shell where JCLI_JUPYTER_SERVER_TOKEN is exported",
        err=True,
    )
    click.echo(command)
