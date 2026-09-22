"""OpenCode skill and capability-aware plugin installation."""

from __future__ import annotations

import json
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
    only_option,
    preflight_gitignore,
    preflight_local_untracked,
    selected_components,
    update_managed_gitignore,
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
@click.option("--skill-dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--remove", is_flag=True, default=False, help="Remove selected components."
)
@pass_ctx
def opencode(
    ctx: CliContext,
    scope: str,
    only: tuple[str, ...],
    skill_dir: Path | None,
    remove: bool,
) -> None:
    """Install OpenCode skill, guards, and notebook output tool."""
    components = selected_components(only)
    if skill_dir is not None and Component.SKILL not in components:
        emit_error(
            "SKILL_DIR_WITHOUT_SKILL",
            "--skill-dir requires selecting skill",
            ctx.use_json,
        )
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL and not ctx.use_json:
        click.echo(
            "Note: OpenCode has no local plugin layer; project paths are managed with exact .gitignore entries.",
            err=True,
        )
    plugin_path = _resolve_opencode_path(scope_value)
    skill_target = _skill_target(scope_value, skill_dir)

    _preflight_skill(skill_target, components, remove, ctx.use_json)
    if scope_value == Scope.LOCAL:
        paths = []
        if components & {Component.HOOK, Component.TOOL}:
            paths.append(plugin_path)
        if Component.SKILL in components:
            paths.append(skill_target)
        preflight_local_untracked(paths, ctx.use_json)
        ignore_dirs = [plugin_path.parent.parent]
        if Component.SKILL in components:
            ignore_dirs.append(skill_target.parent)
        preflight_gitignore(ignore_dirs, ctx.use_json)
    elif scope_value == Scope.PROJECT:
        ignore_dirs = [plugin_path.parent.parent]
        if Component.SKILL in components:
            ignore_dirs.append(skill_target.parent)
        preflight_gitignore(ignore_dirs, ctx.use_json)
    _preflight_plugin(plugin_path, components, remove, ctx.use_json)

    try:
        plugin_changed, capabilities = _apply_plugin(
            plugin_path, components, remove, ctx.use_json
        )
    except OSError as exc:
        emit_error("PLUGIN_WRITE_FAILED", str(exc), ctx.use_json)
    skill_changed = _apply_skill(skill_target, components, remove, ctx.use_json)
    try:
        ignore_changed = _update_ignores(
            scope_value, components, remove, plugin_path, skill_target
        )
    except OSError as exc:
        emit_error("GITIGNORE_WRITE_FAILED", str(exc), ctx.use_json)
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


def _resolve_opencode_path(scope: Scope) -> Path:
    if scope == Scope.USER:
        return Path.home() / ".config" / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    return Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME


def _skill_target(scope: Scope, override: Path | None) -> Path:
    if override is not None:
        root = override.expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve() / "j-cli"
    # OpenCode discovers the cross-host Agent Skills convention in both scopes.
    root = (
        Path.home() / ".agents" / "skills"
        if scope == Scope.USER
        else Path.cwd() / ".agents" / "skills"
    )
    return root / "j-cli"


def _base_source() -> str:
    return (
        resources.files("jupyter_jcli")
        .joinpath("opencode_plugin.js")
        .read_text(encoding="utf-8")
    )


def _capabilities(source: str) -> frozenset[Component]:
    match = _CAPABILITIES_RE.search(source)
    if match is None:
        return frozenset({Component.HOOK, Component.TOOL})
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return frozenset({Component.HOOK, Component.TOOL})
    return frozenset(
        component
        for component in (Component.HOOK, Component.TOOL)
        if raw.get(component.value) is True
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
    path: Path, components: frozenset[Component], remove: bool, use_json: bool
) -> None:
    if not components & {Component.HOOK, Component.TOOL} or not path.exists():
        return
    current = path.read_text(encoding="utf-8")
    if not current.startswith(_OPENCODE_MANAGED_MARKER):
        emit_error(
            "PLUGIN_NOT_MANAGED" if remove else "PLUGIN_CONFLICT",
            f"Refusing to {'remove' if remove else 'overwrite'} non-j-cli plugin: {path}",
            use_json,
        )


def _apply_plugin(
    path: Path, components: frozenset[Component], remove: bool, use_json: bool
) -> tuple[bool, frozenset[Component]]:
    chosen = components & {Component.HOOK, Component.TOOL}
    if not chosen:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        return False, (_capabilities(current) if current else frozenset())
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    before = _capabilities(current) if current else frozenset()
    after = before - chosen if remove else before | chosen
    if not after:
        if path.exists():
            path.unlink()
            changed = True
        else:
            changed = False
    else:
        rendered = _render_source(after)
        changed = current != rendered
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            _warn_duplicate_opencode_plugin(path)
    return changed, after


def _preflight_skill(
    target: Path, components: frozenset[Component], remove: bool, use_json: bool
) -> None:
    if Component.SKILL not in components:
        return
    from .skill import SkillError, preflight_install_skill, preflight_remove_skill

    try:
        (preflight_remove_skill if remove else preflight_install_skill)(target)
    except SkillError as exc:
        emit_error("SKILL_SETUP_FAILED", str(exc), use_json)


def _apply_skill(
    target: Path, components: frozenset[Component], remove: bool, use_json: bool
) -> bool:
    if Component.SKILL not in components:
        return False
    from .skill import SkillError, install_skill, remove_skill

    try:
        return (remove_skill if remove else install_skill)(target)
    except SkillError as exc:
        emit_error("SKILL_SETUP_FAILED", str(exc), use_json)
    return False


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
    changed = update_managed_gitignore(
        plugin_path.parent.parent,
        {Component.HOOK: ["/plugins/jcli.js"], Component.TOOL: ["/plugins/jcli.js"]},
        components,
        enabled,
    )
    if Component.SKILL in components:
        changed = (
            update_managed_gitignore(
                skill_target.parent,
                {Component.SKILL: ["/j-cli/"]},
                components,
                enabled,
            )
            or changed
        )
    return changed


def _warn_duplicate_opencode_plugin(installed_path: Path) -> None:
    project_path = Path.cwd() / ".opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    user_path = Path.home() / ".config" / "opencode" / "plugins" / _OPENCODE_PLUGIN_NAME
    other_path = user_path if installed_path == project_path else project_path
    if other_path.exists():
        click.echo(
            f"warning: {other_path} also exists; OpenCode will load both j-cli plugins",
            err=True,
        )
