"""OpenCode skill and capability-aware plugin installation."""

from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit, emit_error

from .common import (
    Component,
    Scope,
    apply_skill,
    component_ignore_dirs,
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

_OPENCODE_MANAGED_MARKER = "// Managed by j-cli setup opencode."
_OPENCODE_PLUGIN_NAME = "jcli.js"
_CAPABILITIES_RE = re.compile(r"^const enabledCapabilities = (\{.*\})$", re.MULTILINE)


@click.command("opencode")
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
    help="Write project paths and gitignore selected files (default).",
)
@only_option
@force_option
@click.option("--skill-dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--remove", is_flag=True, default=False, help="Remove selected components."
)
@pass_ctx
def opencode(
    ctx: CliContext,
    scope: str,
    only: tuple[str, ...],
    force: bool,
    skill_dir: Path | None,
    remove: bool,
) -> None:
    """Install OpenCode skill, guards, and notebook output tool."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL and not ctx.use_json:
        click.echo(
            "Note: OpenCode has no local plugin layer; project paths are managed with exact .gitignore entries.",
            err=True,
        )
    plugin_path = _resolve_opencode_path(scope_value)
    skill_target = _skill_target(scope_value, skill_dir)
    if not remove:
        _warn_global_opencode_conflicts(
            scope_value, components, plugin_path, skill_target
        )

    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    local_paths: list[Path] = []
    if components & {Component.HOOK, Component.TOOL}:
        local_paths.append(plugin_path)
    if Component.SKILL in components:
        local_paths.append(skill_target)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(local_paths, ctx.use_json)
    ignore_dirs = component_ignore_dirs(
        scope_value, components, plugin_path.parent.parent, skill_target
    )
    preflight_gitignore(ignore_dirs, ctx.use_json)
    try:
        _preflight_plugin(plugin_path, components, remove, ctx.use_json, force)
        plugin_changed, capabilities = _apply_plugin(
            plugin_path, components, remove, ctx.use_json, force
        )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = _update_ignores(
            scope_value, components, remove, plugin_path, skill_target
        )
    except OSError as exc:
        emit_error("PLUGIN_SETUP_FAILED", str(exc), ctx.use_json)
    changed = plugin_changed or skill_changed or ignore_changed
    if Component.SKILL in components and remove and not ctx.use_json:
        click.echo(
            f"Note: removing shared skill {skill_target} affects every host that discovers it.",
            err=True,
        )
    emit(
        {
            "status": ResponseStatus.OK if changed else ResponseStatus.NOOP,
            "components": sorted(component.value for component in components),
            "path": str(plugin_path),
            "plugin_path": str(plugin_path),
            "skill_path": str(skill_target) if Component.SKILL in components else None,
            "capabilities": sorted(component.value for component in capabilities),
            "removed": (1 if remove and plugin_changed else 0)
            + (1 if remove and skill_changed else 0),
            "_human": (
                f"{'Removed' if remove else 'Updated'} OpenCode components: {', '.join(sorted(c.value for c in components))}"
                if changed
                else (
                    f"Nothing to remove: {plugin_path} does not exist."
                    if remove
                    else f"OpenCode plugin is already up to date: {plugin_path}"
                )
            ),
        },
        ctx.use_json,
    )


def _global_plugin_capability(path: Path, component: Component) -> bool | None:
    if not path_exists(path):
        return False
    if not path.exists():
        return True
    source = path.read_text(encoding="utf-8")
    if not source.startswith(_OPENCODE_MANAGED_MARKER):
        return True
    match = _CAPABILITIES_RE.search(source)
    if match is None:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"hook", "tool"}
        or any(not isinstance(raw[key], bool) for key in ("hook", "tool"))
    ):
        return None
    return raw[component.value]


def _warn_global_opencode_conflicts(
    scope: Scope,
    components: frozenset[Component],
    plugin_target: Path,
    skill_target: Path,
) -> None:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    ).expanduser()
    global_plugin = config_home / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    skill_candidates = [
        Path.home() / ".agents" / "skills" / "j-cli",
        Path.home() / ".claude" / "skills" / "j-cli",
        config_home / "opencode" / "skills" / "j-cli",
    ]
    warn_global_conflicts(
        scope,
        components,
        {
            Component.SKILL: skill_target,
            Component.HOOK: plugin_target,
            Component.TOOL: plugin_target,
        },
        {
            Component.SKILL: [(path, path_exists) for path in skill_candidates],
            Component.HOOK: [
                (
                    global_plugin,
                    lambda path: _global_plugin_capability(path, Component.HOOK),
                )
            ],
            Component.TOOL: [
                (
                    global_plugin,
                    lambda path: _global_plugin_capability(path, Component.TOOL),
                )
            ],
        },
    )


def _resolve_opencode_path(scope: Scope) -> Path:
    if scope == Scope.USER:
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        ).expanduser()
        return config_home / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    return Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME


def _skill_target(scope: Scope, override: Path | None) -> Path:
    # OpenCode discovers the cross-host Agent Skills convention in both scopes.
    root = (
        Path.home() / ".agents" / "skills"
        if scope == Scope.USER
        else Path.cwd() / ".agents" / "skills"
    )
    return resolve_skill_target(root, override)


def _base_source() -> str:
    return (
        resources.files("jupyter_jcli")
        .joinpath("opencode_plugin.js")
        .read_text(encoding="utf-8")
    )


def _capabilities(source: str, path: Path, use_json: bool) -> frozenset[Component]:
    match = _CAPABILITIES_RE.search(source)
    if match is None:
        if "const enabledCapabilities =" in source:
            emit_error(
                "PLUGIN_CONFIG_INVALID",
                f"{path}: invalid enabledCapabilities declaration",
                use_json,
            )
        return frozenset({Component.HOOK, Component.TOOL})
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        emit_error(
            "PLUGIN_CONFIG_INVALID",
            f"{path}: invalid enabledCapabilities: {exc}",
            use_json,
        )
    if (
        not isinstance(raw, dict)
        or set(raw) != {"hook", "tool"}
        or any(not isinstance(raw[key], bool) for key in ("hook", "tool"))
    ):
        emit_error(
            "PLUGIN_CONFIG_INVALID",
            f"{path}: enabledCapabilities must contain boolean hook and tool fields",
            use_json,
        )
    return frozenset(
        component
        for component in (Component.HOOK, Component.TOOL)
        if raw[component.value]
    )


def _render_source(components: frozenset[Component]) -> str:
    source = _base_source()
    flags = json.dumps(
        {
            Component.HOOK.value: Component.HOOK in components,
            Component.TOOL.value: Component.TOOL in components,
        },
        separators=(",", ":"),
    )
    return _CAPABILITIES_RE.sub(f"const enabledCapabilities = {flags}", source, count=1)


def _preflight_plugin(
    path: Path,
    components: frozenset[Component],
    remove: bool,
    use_json: bool,
    force: bool = False,
) -> None:
    if not components & {Component.HOOK, Component.TOOL} or not path_exists(path):
        return
    if force and not remove:
        return
    current = path.read_text(encoding="utf-8")
    if not current.startswith(_OPENCODE_MANAGED_MARKER):
        emit_error(
            "PLUGIN_NOT_MANAGED" if remove else "PLUGIN_CONFLICT",
            f"Refusing to {'remove' if remove else 'overwrite'} non-j-cli plugin: {path}",
            use_json,
        )


def _apply_plugin(
    path: Path,
    components: frozenset[Component],
    remove: bool,
    use_json: bool,
    force: bool = False,
) -> tuple[bool, frozenset[Component]]:
    chosen = components & {Component.HOOK, Component.TOOL}
    if not chosen:
        return False, frozenset()
    present = path_exists(path)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    managed = current.startswith(_OPENCODE_MANAGED_MARKER)
    before = (
        _capabilities(current, path, use_json)
        if current and (managed or not force)
        else frozenset()
    )
    after = before - chosen if remove else before | chosen
    if not after:
        if present:
            path.unlink()
            changed = True
        else:
            changed = False
    else:
        rendered = _render_source(after)
        changed = current != rendered or (force and path.is_symlink())
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            if force and path.is_symlink():
                path.unlink()
            path.write_text(rendered, encoding="utf-8")
            _warn_duplicate_opencode_plugin(path)
    return changed, after


def _update_ignores(
    scope: Scope,
    components: frozenset[Component],
    remove: bool,
    plugin_path: Path,
    skill_target: Path,
) -> bool:
    if scope == Scope.USER:
        return False
    enabled = scope == Scope.LOCAL and not remove
    changed = False
    if components & {Component.HOOK, Component.TOOL}:
        changed = update_managed_gitignore(
            plugin_path.parent.parent,
            {
                Component.HOOK: ["/plugins/jcli.js"],
                Component.TOOL: ["/plugins/jcli.js"],
            },
            components,
            enabled,
        )
    changed = update_skill_ignore(skill_target, scope, remove, components) or changed
    return changed


def _warn_duplicate_opencode_plugin(installed_path: Path) -> None:
    project_path = Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    user_path = _resolve_opencode_path(Scope.USER)
    if installed_path != user_path:
        return
    other_path = project_path
    if other_path.exists():
        click.echo(
            f"warning: {other_path} also exists; OpenCode will load both j-cli plugins",
            err=True,
        )
