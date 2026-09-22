"""Claude Code and Codex managed JSON hook installation."""

import json
import os
import re
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
    selected_components,
    update_managed_gitignore,
    validate_force,
    validate_skill_dir,
    warn_global_conflicts,
)
from .mcp import manage_claude_mcp, manage_codex_mcp

# ---------------------------------------------------------------------------
# Managed hook blocks
#
# Each block descriptor has:
#   event     - hook event type ("PreToolUse" or "PostToolUse")
#   matcher   - tool matcher string
#   platforms - list of platforms this block applies to (e.g. ["claude", "codex"])
#   entry     - the hook entry dict to install (must contain _jcli_managed key)
#               command may contain {platform_flag} placeholder substituted at install
#   legacy    - frozenset of old _jcli_managed values to replace on upgrade
# ---------------------------------------------------------------------------

_MANAGED_KEY = "_jcli_managed"

_MANAGED_BLOCKS: list[dict] = [
    {
        "event": "PreToolUse",
        "matcher": "Bash",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-exec-guard{platform_flag}",
            "_jcli_managed": "notebook-exec-guard",
        },
        "legacy": frozenset({"nbconvert-guard"}),
    },
    {
        "event": "PreToolUse",
        "matcher": "Edit|Write",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard-pre{platform_flag}",
            "_jcli_managed": "pair-drift-guard-pre",
        },
        "legacy": frozenset({"pair-drift-guard"}),
    },
    {
        "event": "PreToolUse",
        "matcher": "NotebookEdit",
        "platforms": ["claude"],  # Codex has no NotebookEdit tool
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-edit-guard{platform_flag}",
            "_jcli_managed": "notebook-edit-guard",
        },
        "legacy": frozenset({"pair-drift-guard-notebook"}),
    },
    {
        "event": "PostToolUse",
        "matcher": "Edit|Write",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard-post{platform_flag}",
            "_jcli_managed": "pair-drift-guard-post",
        },
        "legacy": frozenset(),
    },
    {
        "event": "PreToolUse",
        "matcher": "Bash",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks python-run-guard{platform_flag}",
            "_jcli_managed": "python-run-guard",
        },
        "legacy": frozenset(),
    },
]

# DSH consumes Claude-shaped JSON, but its tool names are lower-case and its
# bridge needs an explicit platform selector for the payload differences.
_DSH_MATCHERS = {
    "Bash": "^bash$",
    "Edit|Write": "^(edit|write)$",
}
_DSH_MANAGED_BLOCKS: list[dict] = [
    {
        **block,
        "matcher": _DSH_MATCHERS[block["matcher"]],
        "platforms": ["dsh"],
    }
    for block in _MANAGED_BLOCKS
    if block["matcher"] in _DSH_MATCHERS
]


def _managed_values(blocks: list[dict]) -> frozenset[str]:
    return frozenset(
        val
        for block in blocks
        for val in ({block["entry"][_MANAGED_KEY]} | block["legacy"])
    )


# All managed values across all blocks (current + legacy) — used for upgrade detection
_ALL_MANAGED_VALS = _managed_values(_MANAGED_BLOCKS)
_DSH_MANAGED_VALS = _managed_values(_DSH_MANAGED_BLOCKS)


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
):
    """Install Claude skill, native hooks, and notebook-output MCP tool."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    path = _resolve_claude_path(scope)
    skill_target = _skill_target("claude", scope_value, skill_dir)
    if not remove:
        _warn_claude_global_conflicts(scope_value, components, path, skill_target)
    if Component.HOOK in components and path.exists():
        _load_settings(path, ctx.use_json)
    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    ignore_dirs = component_ignore_dirs(scope_value, components, None, skill_target)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(
            [skill_target] if Component.SKILL in components else [], ctx.use_json
        )
    preflight_gitignore(ignore_dirs, ctx.use_json)
    try:
        tool_result = (
            manage_claude_mcp(scope, Path.cwd(), remove, ctx.use_json, force)
            if Component.TOOL in components
            else "skipped"
        )
        hook_changed, removed_hooks = (
            _install_or_remove("claude", path, remove, ctx)
            if Component.HOOK in components
            else (False, 0)
        )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = _update_skill_ignore(
            skill_target, scope_value, remove, components
        )
    except OSError as exc:
        emit_error("SETUP_WRITE_FAILED", str(exc), ctx.use_json)
    _emit_setup_result(
        "Claude",
        components,
        remove,
        path,
        skill_target,
        tool_result,
        hook_changed,
        removed_hooks,
        skill_changed,
        ignore_changed,
        ctx,
    )


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
):
    """Install Codex skill, native hooks, and notebook-output MCP tool."""
    components = selected_components(only)
    validate_force(force, remove, ctx.use_json)
    validate_skill_dir(components, skill_dir, ctx.use_json)
    scope_value = Scope(scope)
    if scope_value == Scope.LOCAL and not ctx.use_json:
        click.echo(
            "Note: Codex has no native local layer; project paths are managed with exact .gitignore entries.",
            err=True,
        )
    path = _resolve_codex_path(scope)
    skill_target = _skill_target("codex", scope_value, skill_dir)
    if not remove:
        _warn_codex_global_conflicts(scope_value, components, path, skill_target)
    if Component.HOOK in components and path.exists():
        _load_settings(path, ctx.use_json)
    preflight_skill(skill_target, components, remove, ctx.use_json, force)
    local_paths: list[Path] = []
    if Component.HOOK in components:
        local_paths.append(path)
    if Component.TOOL in components:
        local_paths.append(path.parent / "config.toml")
    if Component.SKILL in components:
        local_paths.append(skill_target)
    if scope_value == Scope.LOCAL:
        preflight_local_untracked(local_paths, ctx.use_json)
    ignore_dirs = component_ignore_dirs(
        scope_value, components, path.parent, skill_target
    )
    preflight_gitignore(ignore_dirs, ctx.use_json)
    try:
        tool_result = (
            manage_codex_mcp(scope, Path.cwd(), remove, ctx.use_json, force)
            if Component.TOOL in components
            else "skipped"
        )
        hook_changed, removed_hooks = (
            _install_or_remove("codex", path, remove, ctx)
            if Component.HOOK in components
            else (False, 0)
        )
        skill_changed = apply_skill(
            skill_target, components, remove, ctx.use_json, force
        )
        ignore_changed = _update_codex_ignores(
            scope_value, remove, components, skill_target
        )
    except OSError as exc:
        emit_error("SETUP_WRITE_FAILED", str(exc), ctx.use_json)
    _emit_setup_result(
        "Codex",
        components,
        remove,
        path,
        skill_target,
        tool_result,
        hook_changed,
        removed_hooks,
        skill_changed,
        ignore_changed,
        ctx,
    )


def _install_or_remove(
    platform: str, path: Path, remove: bool, ctx: CliContext
) -> tuple[bool, int]:
    """Install or remove managed hooks and return changed/removed counts."""
    if remove:
        if not path.exists():
            return False, 0
        settings = _load_settings(path, ctx.use_json)
        removed = _remove_managed_hooks(settings)
        if "hooks" in settings:
            for event_key in list(settings["hooks"]):
                if not settings["hooks"].get(event_key):
                    settings["hooks"].pop(event_key, None)
            if not settings["hooks"]:
                del settings["hooks"]
        if removed:
            if settings:
                _write_settings(path, settings)
            else:
                path.unlink()
        return removed > 0, removed

    path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "codex":
        _ensure_codex_feature_flag(path)
    before = path.read_text(encoding="utf-8") if path.exists() else None
    settings = _load_settings(path, ctx.use_json)
    for block_desc in _MANAGED_BLOCKS:
        if platform in block_desc.get("platforms", []):
            _merge_hook(settings, block_desc, platform)
    rendered = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if rendered == before:
        return False, 0
    path.write_text(rendered, encoding="utf-8")
    return True, 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_codex_feature_flag(path: Path) -> None:
    """Check that codex_hooks feature flag is enabled in a .codex/config.toml.

    Searches for ``[features]`` section line followed by ``codex_hooks = true``.
    Prints a warning to stderr if the flag is missing.  Does NOT modify the file.
    """
    scope_dir = path.parent
    config_toml = scope_dir / "config.toml"

    if not config_toml.exists():
        click.echo(
            f"warning: {config_toml} not found — "
            f"Codex hooks require '[features]\ncodex_hooks = true' in config.toml",
            err=True,
        )
        return

    text = config_toml.read_text(encoding="utf-8")
    # Scan for codex_hooks = true under [features] section.
    # Line-based text scan avoids pulling in a TOML parser for a single check.
    # Limitation: does not handle multi-line TOML strings containing "[features]"
    # as literal text.  In practice this is vanishingly rare and the function
    # only warns (does not block), so the trade-off is acceptable.
    in_features = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()  # strip inline comments
        if stripped == "[features]":
            in_features = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = False
            continue
        if in_features and re.match(r"^codex_hooks\s*=\s*true\b", stripped):
            return  # flag found

    click.echo(
        f"warning: codex_hooks feature flag not enabled in {config_toml} — "
        f"add '[features]\ncodex_hooks = true' to activate hooks",
        err=True,
    )


def _read_global_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _global_hooks_present(path: Path) -> bool:
    settings = _read_global_json(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("hooks must be an object")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise TypeError("hook event must be an array")
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks", [])
            if not isinstance(entries, list):
                raise TypeError("hook group must contain an array")
            if any(
                isinstance(entry, dict) and entry.get(_MANAGED_KEY) in _ALL_MANAGED_VALS
                for entry in entries
            ):
                return True
    return False


def _global_claude_mcp_present(path: Path) -> bool:
    servers = _read_global_json(path).get("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError("mcpServers must be an object")
    return "jcli-notebook-output" in servers


def _global_codex_mcp_present(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return bool(
        re.search(
            r"(?m)^\s*\[mcp_servers\.(?:jcli-notebook-output|\"jcli-notebook-output\"|'jcli-notebook-output')\]\s*(?:#.*)?$",
            text,
        )
    )


def _warn_claude_global_conflicts(
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
                (Path.home() / ".claude" / "settings.json", _global_hooks_present)
            ],
            Component.TOOL: [
                (Path.home() / ".claude.json", _global_claude_mcp_present)
            ],
        },
    )


def _warn_codex_global_conflicts(
    scope: Scope,
    components: frozenset[Component],
    hook_target: Path,
    skill_target: Path,
) -> None:
    codex_home = _codex_home()
    warn_global_conflicts(
        scope,
        components,
        {
            Component.SKILL: skill_target,
            Component.HOOK: hook_target,
            Component.TOOL: Path.cwd() / ".codex" / "config.toml",
        },
        {
            Component.SKILL: [(codex_home / "skills" / "j-cli", path_exists)],
            Component.HOOK: [(codex_home / "hooks.json", _global_hooks_present)],
            Component.TOOL: [(codex_home / "config.toml", _global_codex_mcp_present)],
        },
    )


def _resolve_claude_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".claude" / "settings.json"
    if s == Scope.PROJECT:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


def _resolve_codex_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return _codex_home() / "hooks.json"
    # Codex only reads hooks.json — there is no hooks.local.json layer.
    # Both --project and --local write to ./.codex/hooks.json.
    return Path.cwd() / ".codex" / "hooks.json"


def _load_settings(path: Path, use_json: bool) -> dict:
    """Load existing settings or return an empty dict."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        emit_error("SETTINGS_INVALID", f"{path}: {exc}", use_json)
        raise SystemExit(1) from exc
    if not isinstance(settings, dict):
        emit_error("SETTINGS_INVALID", f"{path}: expected a JSON object", use_json)
    return settings


def _merge_hook(settings: dict, block_desc: dict, platform: str) -> None:
    """Merge one managed hook block into settings for the given platform.

    Only inserts/updates if block_desc['platforms'] includes *platform*.
    Substitutes {platform_flag} in the command string:
      "claude" -> "" (backward compat, no flag)
      "codex"  -> " --platform codex"
      "dsh"    -> " --platform dsh"
    """
    target_event: str = block_desc.get("event", "PreToolUse")
    target_matcher: str = block_desc["matcher"]
    current_entry: dict = block_desc["entry"]
    current_val: str = current_entry[_MANAGED_KEY]
    all_vals: frozenset[str] = frozenset({current_val}) | block_desc["legacy"]

    # Substitute platform flag (shallow-copy to avoid mutating descriptors).
    platform_flag = {
        "codex": " --platform codex",
        "dsh": " --platform dsh",
    }.get(platform, "")
    current_entry = {
        **current_entry,
        "command": current_entry["command"].replace("{platform_flag}", platform_flag),
    }

    hooks_map: dict = settings.setdefault("hooks", {})
    event_list: list = hooks_map.setdefault(target_event, [])

    placed = False
    for block in event_list:
        if not isinstance(block, dict) or block.get("matcher") != target_matcher:
            continue
        inner: list = block.get("hooks", [])
        new_inner = []
        for entry in inner:
            if isinstance(entry, dict) and entry.get(_MANAGED_KEY) in all_vals:
                if not placed:
                    new_inner.append(current_entry)
                    placed = True
                # else: drop stale duplicate
            else:
                new_inner.append(entry)
        block["hooks"] = new_inner

    if not placed:
        event_list.append({"matcher": target_matcher, "hooks": [current_entry]})


def _write_settings(path: Path, settings: dict) -> None:
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _remove_managed_hooks(
    settings: dict, managed_values: frozenset[str] | None = None
) -> int:
    """Remove managed entries from settings["hooks"] (all event types).

    ``managed_values`` narrows removal for integrations that own a separate
    settings file, such as the DSH bridge. Empty event-type blocks are dropped;
    the caller is responsible for pruning empty ``hooks`` / top-level dicts.
    """
    values = _ALL_MANAGED_VALS if managed_values is None else managed_values
    hooks_map = settings.get("hooks")
    if not hooks_map:
        return 0

    removed = 0
    for event_key in list(hooks_map.keys()):
        event_list = hooks_map.get(event_key)
        if not event_list:
            continue

        new_event_list = []
        for block in event_list:
            if not isinstance(block, dict):
                new_event_list.append(block)
                continue
            inner = block.get("hooks", [])
            new_inner = [
                entry
                for entry in inner
                if not (isinstance(entry, dict) and entry.get(_MANAGED_KEY) in values)
            ]
            removed += len(inner) - len(new_inner)
            if new_inner:
                new_event_list.append({**block, "hooks": new_inner})
            # else: block is empty after pruning — drop it
        hooks_map[event_key] = new_event_list

    return removed


def _install_dsh_hooks(settings: dict) -> None:
    """Install the four DSH bridge hook blocks into a JSON settings map."""
    # Remove stale matcher/command variants before adding the current lower-case
    # DSH rows, so upgrades cannot leave duplicate guards behind.
    _remove_dsh_hooks(settings)
    for block_desc in _DSH_MANAGED_BLOCKS:
        _merge_hook(settings, block_desc, "dsh")


def _remove_dsh_hooks(settings: dict) -> int:
    """Remove only DSH-managed hook blocks from a JSON settings map."""
    return _remove_managed_hooks(settings, _DSH_MANAGED_VALS)


def _emit_setup_result(
    platform: str,
    components: frozenset[Component],
    remove: bool,
    hook_path: Path,
    skill_path: Path,
    tool_result: str,
    hook_changed: bool,
    removed_hooks: int,
    skill_changed: bool,
    ignore_changed: bool,
    ctx: CliContext,
) -> None:
    changed = (
        hook_changed
        or skill_changed
        or ignore_changed
        or tool_result in {"installed", "removed"}
    )
    if remove and Component.SKILL in components and not ctx.use_json:
        click.echo(
            f"Note: removing shared skill {skill_path} affects every host that discovers it.",
            err=True,
        )
    emit(
        {
            "status": ResponseStatus.OK if changed else ResponseStatus.NOOP,
            "components": sorted(component.value for component in components),
            "path": str(hook_path),
            "hook_path": str(hook_path),
            "skill_path": str(skill_path) if Component.SKILL in components else None,
            "tool_result": tool_result,
            "removed": removed_hooks
            + (1 if remove and skill_changed else 0)
            + (1 if tool_result == "removed" else 0),
            "_human": (
                f"{'Removed' if remove else 'Updated'} {platform} components: {', '.join(sorted(c.value for c in components))}"
                if changed
                else (
                    f"Nothing to remove: {hook_path} does not exist or has no selected managed components."
                    if remove
                    else f"{platform} selected components are already up to date."
                )
            ),
        },
        ctx.use_json,
    )


def _skill_target(platform: str, scope: Scope, override: Path | None) -> Path:
    if override is not None:
        root = override.expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve() / "j-cli"
    if platform == "claude":
        root = (
            Path.home() / ".claude" / "skills"
            if scope == Scope.USER
            else Path.cwd() / ".claude" / "skills"
        )
    elif scope == Scope.USER:
        root = _codex_home() / "skills"
    else:
        root = Path.cwd() / ".agents" / "skills"
    return root / "j-cli"


def _update_skill_ignore(
    target: Path, scope: Scope, remove: bool, components: frozenset[Component]
) -> bool:
    if Component.SKILL not in components or scope == Scope.USER:
        return False
    return update_managed_gitignore(
        target.parent,
        {Component.SKILL: ["/j-cli/"]},
        components,
        scope == Scope.LOCAL and not remove,
    )


def _update_codex_ignores(
    scope: Scope, remove: bool, components: frozenset[Component], skill_target: Path
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
    if Component.SKILL in components:
        changed = (
            _update_skill_ignore(skill_target, scope, remove, components) or changed
        )
    return changed


def _codex_home() -> Path:
    return (
        Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        .expanduser()
        .resolve()
    )
