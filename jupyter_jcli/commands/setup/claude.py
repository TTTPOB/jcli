"""Claude Code host orchestration for ``j-cli setup claude``."""

import json
from pathlib import Path

import click

from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit_error

from .common import (
    Component,
    NativeSetupResult,
    Scope,
    apply_skill,
    component_ignore_dirs,
    emit_native_setup_result,
    force_option,
    only_option,
    path_exists,
    preflight_gitignore,
    preflight_local_untracked,
    preflight_skill,
    resolve_skill_target,
    selected_components,
    update_skill_ignore,
    validate_force,
    validate_skill_dir,
    warn_global_conflicts,
)
from .hooks import install_or_remove_hooks, load_settings, managed_hooks_present
from .mcp import manage_claude_mcp


@click.command("claude")
@click.option(
    "--user", "scope", flag_value=Scope.USER.value, help="Write user configuration."
)
@click.option(
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    help="Write shared project configuration.",
)
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    default=True,
    help="Write native local configuration (default).",
)
@only_option
@force_option
@click.option("--skill-dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--remove", is_flag=True, default=False, help="Remove selected components."
)
@pass_ctx
def claude(
    ctx: CliContext,
    scope: str,
    only: tuple[str, ...],
    force: bool,
    skill_dir: Path | None,
    remove: bool,
) -> None:
    """Install Claude skill, native hooks, and notebook-output MCP tool."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    hook_path = _resolve_hook_path(scope_value)
    skill_target = _skill_target(scope_value, skill_dir)
    if not remove:
        _warn_global_conflicts(scope_value, components, hook_path, skill_target)
    if Component.HOOK in components and hook_path.exists():
        load_settings(hook_path, ctx.use_json)
    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(
            [skill_target] if Component.SKILL in components else [], ctx.use_json
        )
    preflight_gitignore(
        component_ignore_dirs(scope_value, components, None, skill_target), ctx.use_json
    )
    try:
        tool_result = (
            manage_claude_mcp(scope, Path.cwd(), remove, ctx.use_json, force)
            if Component.TOOL in components
            else "skipped"
        )
        hook_changed, removed_hooks = (
            install_or_remove_hooks("claude", hook_path, remove, ctx.use_json, force)
            if Component.HOOK in components
            else (False, 0)
        )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = update_skill_ignore(
            skill_target, scope_value, remove, components
        )
    except OSError as exc:
        emit_error("SETUP_WRITE_FAILED", str(exc), ctx.use_json)
    emit_native_setup_result(
        NativeSetupResult(
            host_name="Claude",
            components=components,
            remove=remove,
            hook_path=hook_path,
            skill_path=skill_target,
            tool_result=tool_result,
            hook_changed=hook_changed,
            removed_hooks=removed_hooks,
            skill_changed=skill_changed,
            ignore_changed=ignore_changed,
        ),
        ctx.use_json,
    )


def _resolve_hook_path(scope: Scope) -> Path:
    if scope == Scope.USER:
        return Path.home() / ".claude" / "settings.json"
    if scope == Scope.PROJECT:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


def _skill_target(scope: Scope, override: Path | None) -> Path:
    root = (
        Path.home() / ".claude" / "skills"
        if scope == Scope.USER
        else Path.cwd() / ".claude" / "skills"
    )
    return resolve_skill_target(root, override)


def _read_global_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _global_mcp_present(path: Path) -> bool:
    servers = _read_global_json(path).get("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError("mcpServers must be an object")
    return "jcli-notebook-output" in servers


def _warn_global_conflicts(
    scope: Scope,
    components: frozenset[Component],
    hook_target: Path,
    skill_target: Path,
) -> None:
    warn_global_conflicts(
        scope,
        components,
        {
            Component.SKILL: skill_target,
            Component.HOOK: hook_target,
            Component.TOOL: Path.cwd() / ".mcp.json",
        },
        {
            Component.SKILL: [
                (Path.home() / ".claude" / "skills" / "j-cli", path_exists)
            ],
            Component.HOOK: [
                (
                    Path.home() / ".claude" / "settings.json",
                    managed_hooks_present,
                )
            ],
            Component.TOOL: [(Path.home() / ".claude.json", _global_mcp_present)],
        },
    )
