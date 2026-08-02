"""OpenCode plugin installation for setup commands."""

from importlib import resources
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.commands.setup_common import Scope
from jupyter_jcli.output import emit, emit_error

_OPENCODE_MANAGED_MARKER = "// Managed by j-cli setup opencode."
_OPENCODE_PLUGIN_NAME = "jcli.js"


@click.command("opencode")
@click.option(
    "--user",
    "scope",
    flag_value=Scope.USER.value,
    help="Write to ~/.config/opencode/plugins/jcli.js",
)
@click.option(
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    default=True,
    help="Write to ./.opencode/plugins/jcli.js (default)",
)
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    help="Alias for --project (OpenCode has no local plugin layer)",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove the j-cli managed OpenCode plugin.",
)
@pass_ctx
def opencode(ctx: CliContext, scope: str, remove: bool) -> None:
    """Install the OpenCode plugin for notebook guards and pair synchronization."""
    path = _resolve_opencode_path(scope)
    if Scope(scope) == Scope.LOCAL:
        click.echo(
            "Note: OpenCode has no local plugin layer; --local writes to "
            "./.opencode/plugins/jcli.js",
            err=True,
        )
    _install_or_remove_opencode(path, remove, ctx)


def _resolve_opencode_path(scope: str) -> Path:
    if Scope(scope) == Scope.USER:
        return Path.home() / ".config" / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    return Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME


def _opencode_plugin_source() -> str:
    return (
        resources.files("jupyter_jcli")
        .joinpath("opencode_plugin.js")
        .read_text(encoding="utf-8")
    )


def _install_or_remove_opencode(path: Path, remove: bool, ctx: CliContext) -> None:
    if remove:
        if not path.exists():
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "path": str(path),
                    "_human": f"Nothing to remove: {path} does not exist.",
                },
                ctx.use_json,
            )
            return

        current = path.read_text(encoding="utf-8")
        if not current.startswith(_OPENCODE_MANAGED_MARKER):
            emit_error(
                "PLUGIN_NOT_MANAGED",
                f"Refusing to remove non-j-cli plugin: {path}",
                ctx.use_json,
            )
        path.unlink()
        emit(
            {
                "status": ResponseStatus.OK,
                "removed": 1,
                "path": str(path),
                "_human": f"Removed j-cli OpenCode plugin from {path}.",
            },
            ctx.use_json,
        )
        return

    source = _opencode_plugin_source()
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if not current.startswith(_OPENCODE_MANAGED_MARKER):
            emit_error(
                "PLUGIN_CONFLICT",
                f"Refusing to overwrite non-j-cli plugin: {path}",
                ctx.use_json,
            )
        if current == source:
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "path": str(path),
                    "_human": f"OpenCode plugin is already up to date: {path}",
                },
                ctx.use_json,
            )
            return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    _warn_duplicate_opencode_plugin(path)
    emit(
        {
            "status": ResponseStatus.OK,
            "path": str(path),
            "_human": f"Wrote OpenCode plugin to {path}",
        },
        ctx.use_json,
    )


def _warn_duplicate_opencode_plugin(installed_path: Path) -> None:
    project_path = Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    user_path = Path.home() / ".config" / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    other_path = user_path if installed_path == project_path else project_path
    if other_path.exists():
        click.echo(
            f"warning: {other_path} also exists; OpenCode will load both j-cli plugins",
            err=True,
        )
