"""Codex host orchestration for ``j-cli setup codex``."""

import re
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
    update_managed_gitignore,
    update_skill_ignore,
    validate_force,
    validate_skill_dir,
    warn_global_conflicts,
)
from .hooks import install_or_remove_hooks, load_settings, managed_hooks_present
from .mcp import manage_codex_mcp
from .paths import codex_home


@click.command("codex")
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
    help="Write project paths with exact ignores (default).",
)
@only_option
@force_option
@click.option("--skill-dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--remove", is_flag=True, default=False, help="Remove selected components."
)
@pass_ctx
def codex(
    ctx: CliContext,
    scope: str,
    only: tuple[str, ...],
    force: bool,
    skill_dir: Path | None,
    remove: bool,
) -> None:
    """Install Codex skill, native hooks, and notebook-output MCP tool."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL and not ctx.use_json:
        click.echo(
            "Note: Codex has no native local layer; project paths are managed "
            "with exact .gitignore entries.",
            err=True,
        )
    hook_path = _resolve_hook_path(scope_value)
    skill_target = _skill_target(scope_value, skill_dir)
    if not remove:
        _warn_global_conflicts(scope_value, components, hook_path, skill_target)
    if Component.HOOK in components and hook_path.exists():
        load_settings(hook_path, ctx.use_json)
    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    local_paths: list[Path] = []
    if Component.HOOK in components:
        local_paths.append(hook_path)
    if Component.TOOL in components:
        local_paths.append(hook_path.parent / "config.toml")
    if Component.SKILL in components:
        local_paths.append(skill_target)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(local_paths, ctx.use_json)
    preflight_gitignore(
        component_ignore_dirs(scope_value, components, hook_path.parent, skill_target),
        ctx.use_json,
    )
    try:
        tool_result = (
            manage_codex_mcp(scope, Path.cwd(), remove, ctx.use_json, force)
            if Component.TOOL in components
            else "skipped"
        )
        if Component.HOOK in components and not remove:
            _warn_feature_flag(hook_path.parent / "config.toml")
        hook_changed, removed_hooks = (
            install_or_remove_hooks("codex", hook_path, remove, ctx.use_json, force)
            if Component.HOOK in components
            else (False, 0)
        )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = _update_ignores(scope_value, remove, components, skill_target)
    except OSError as exc:
        emit_error("SETUP_WRITE_FAILED", str(exc), ctx.use_json)
    emit_native_setup_result(
        NativeSetupResult(
            host_name="Codex",
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
        return codex_home() / "hooks.json"
    return Path.cwd() / ".codex" / "hooks.json"


def _skill_target(scope: Scope, override: Path | None) -> Path:
    root = (
        codex_home() / "skills"
        if scope == Scope.USER
        else Path.cwd() / ".agents" / "skills"
    )
    return resolve_skill_target(root, override)


def _warn_feature_flag(config_toml: Path) -> None:
    """Warn when Codex's native hook feature is not visibly enabled."""
    if not config_toml.exists():
        click.echo(
            f"warning: {config_toml} not found — Codex hooks require "
            "'[features]\ncodex_hooks = true' in config.toml",
            err=True,
        )
        return
    in_features = False
    for line in config_toml.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped == "[features]":
            in_features = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = False
            continue
        if in_features and re.match(r"^codex_hooks\s*=\s*true\b", stripped):
            return
    click.echo(
        f"warning: codex_hooks feature flag not enabled in {config_toml} — "
        "add '[features]\ncodex_hooks = true' to activate hooks",
        err=True,
    )


def _global_mcp_present(path: Path) -> bool:
    if not path.exists():
        return False
    return bool(
        re.search(
            r"(?m)^\s*\[mcp_servers\."
            r"(?:jcli-notebook-output|\"jcli-notebook-output\"|'jcli-notebook-output')"
            r"\]\s*(?:#.*)?$",
            path.read_text(encoding="utf-8"),
        )
    )


def _warn_global_conflicts(
    scope: Scope,
    components: frozenset[Component],
    hook_target: Path,
    skill_target: Path,
) -> None:
    home = codex_home()
    warn_global_conflicts(
        scope,
        components,
        {
            Component.SKILL: skill_target,
            Component.HOOK: hook_target,
            Component.TOOL: Path.cwd() / ".codex" / "config.toml",
        },
        {
            Component.SKILL: [(home / "skills" / "j-cli", path_exists)],
            Component.HOOK: [(home / "hooks.json", managed_hooks_present)],
            Component.TOOL: [(home / "config.toml", _global_mcp_present)],
        },
    )


def _update_ignores(
    scope: Scope,
    remove: bool,
    components: frozenset[Component],
    skill_target: Path,
) -> bool:
    if scope == Scope.USER:
        return False
    enabled = scope == Scope.LOCAL and not remove
    changed = False
    if components & {Component.HOOK, Component.TOOL}:
        changed = update_managed_gitignore(
            Path.cwd() / ".codex",
            {Component.HOOK: ["/hooks.json"], Component.TOOL: ["/config.toml"]},
            components,
            enabled,
        )
    changed = update_skill_ignore(skill_target, scope, remove, components) or changed
    return changed
